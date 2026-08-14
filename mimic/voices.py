"""Sichere Stimmprofile und die kleine Aussprachekorrektur."""

from __future__ import annotations

import json
import os
import re
import stat
import wave
from dataclasses import dataclass
from pathlib import Path

from .effekte import (BREITE_MAX, BREITE_MIN, EFFEKTE, FORMANT_MAX, FORMANT_MIN,
                      HALL_MAX, HALL_MIN, KRUEMEL_MAX, KRUEMEL_MIN, RASTER_MAX,
                      RASTER_MIN, STREUUNG_MAX, STREUUNG_MIN, TONHOEHE_MAX,
                      TONHOEHE_MIN, VERZERRUNG_MAX, VERZERRUNG_MIN, ist_effekt)

VOICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_WAV_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024
MAX_SETTINGS_BYTES = 4 * 1024

# Phase 0 hat `en` als Vorgabe gemessen, auch fuer deutschen Text -- mit `de`
# bekommen englische Fachbegriffe deutsche Phonetik. Das gilt fuer eine
# DEUTSCHE Referenz. Bei einer englischen entscheidet nicht mehr der Tag,
# sondern `speaker_scale`: 1.5 traegt den Akzent der Referenz voll ins Deutsche.
DEFAULT_SPRACHE = "en"
DEFAULT_SCALE = 1.5
SPRACHEN = frozenset({"de", "en"})
# Grenzen, keine Empfehlung. Unter 0.5 loest sich die Stimme von der Referenz,
# ueber 2.0 kippt sie ins Uebersteuerte -- beides gemessen am 2026-08-09.
SCALE_MIN, SCALE_MAX = 0.5, 2.0


class VoiceError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class VoiceProfile:
    name: str
    wav_path: str
    prompt_text: str
    gain: float = 1.0
    language: str = DEFAULT_SPRACHE
    speaker_scale: float = DEFAULT_SCALE
    effekt: str = ""            # leer = unbearbeitet, sonst ein Name aus effekte.EFFEKTE
    tonhoehe: float = 0.0       # Halbtoene, dauerhaft fuer diese Stimme
    streuung: float = 0.0       # Halbtoene, um die eine Silbe daneben liegen darf
    raster: float = 0.0         # 0 aus, 1 zwingt jede Silbe auf den Halbton
    formant: float = 0.0        # Halbtoene, um die die Klangfarbe steigt
    hall: float = 0.0           # 0 trocken, 1 voller Nachhall
    verzerrung: float = 0.0     # 0 sauber, 1 uebersteuert
    kruemel: float = 0.0        # 0 glatt, 1 vier Bit und gehaltene Proben
    breite: float = 0.0         # 0 eine Stimme, 1 vier verstimmte Kopien


# Zielpegel der Ausgabe. Gemessen am 2026-08-05: Mimic liefert den Pegel der
# Referenz getreu wieder -- Klon -29.6, Referenz -30.9, echte Aufnahmen -30.1
# dBFS RMS. Leise ist also die Quelle, nicht das Modell. Deshalb wird die
# Verstaerkung je Stimme EINMAL aus der Referenz abgeleitet statt pro Aeusserung
# normalisiert: das ist streamingtauglich und pumpt nicht zwischen Chunks.
# -18 statt -23: bei -23 blieb es hoerbar zu leise. Weiter aufdrehen geht nur
# mit Begrenzer, weil der Peak dann schon bei -1.9 dBFS lag -- reine Verstaerkung
# wuerde clippen. Siehe den weichen Anschlag in worker.tensor_to_pcm.
ZIEL_RMS_DBFS = -18.0
MAX_GAIN = 8.0          # Obergrenze, damit eine fast stumme Referenz nicht Rauschen hochzieht


# Die Verstaerkung haengt nur an der Referenzdatei, aendert sich also nie --
# aber sie wurde bei JEDER Anfrage neu berechnet: eine Python-Schleife ueber
# 710 656 Samples, gemessen 22 ms. Bei einer Basis-TTFA von 230 ms sind das
# rund zehn Prozent, die niemand braucht. Schluessel ist (Geraet, Inode,
# Groesse, mtime_ns) statt des Pfades: der Pfad ist /proc/self/fd/N und damit
# je Aufruf ein anderer, waehrend der Inode dieselbe Datei festhaelt.
_GAIN_CACHE: dict[tuple, float] = {}


def _reference_gain(wav_path: str) -> float:
    """Faktor, der die Referenz auf ZIEL_RMS_DBFS hebt. Ergebnis wird gemerkt."""
    try:
        info = os.stat(wav_path)
        schluessel = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except OSError:
        schluessel = None
    if schluessel is not None and schluessel in _GAIN_CACHE:
        return _GAIN_CACHE[schluessel]
    wert = _reference_gain_berechnen(wav_path)
    if schluessel is not None:
        _GAIN_CACHE[schluessel] = wert
    return wert


def _reference_gain_berechnen(wav_path: str) -> float:
    import array
    import math
    with wave.open(wav_path, "rb") as wav:
        if wav.getsampwidth() != 2:
            return 1.0
        samples = array.array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    if not samples:
        return 1.0
    quadratsumme = sum(float(value) * value for value in samples)
    rms = math.sqrt(quadratsumme / len(samples)) / 32768.0
    if rms <= 0:
        return 1.0
    return min(10 ** (ZIEL_RMS_DBFS / 20) / rms, MAX_GAIN)


_SATZENDE = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")
_SATZENDE_AM_SCHLUSS = re.compile(r"[.!?…][\"')\]]*$")
# 20, nicht 12. Gemessen am 2026-08-05: ein Satz mit 14 Zeichen kam in 4 von 22
# Generierungen ohne Sprache zurueck, Saetze mit 19 bis 21 Zeichen in 0 von 24.
# Der Worker faengt den Rest per Wiederholung ab (worker.STUMM_PEAK), aber die
# kostet dort hoerbare Totzeit -- billiger ist, den kurzen Satz an seinen
# Nachbarn zu haengen. Preis: an solchen Stellen entfaellt die Atempause.
MIN_SATZ_ZEICHEN = 20
# Ein Generierungsaufruf bleibt so in der Laenge, die das Modell sauber traegt.
# 80 statt 250: der Nachtlauf vom 2026-08-10 (forschung/journal.jsonl) hat den
# Sweep 250->160->120->80 gemessen -- bei 250 verschluckte dots.tts in langen
# Kommatexten stochastisch ganze Chunks (Wortfehlerrate 0.31/0.50 bei
# unauffaelliger Sprecher-Aehnlichkeit), ab 160 abwaerts trat das in 12 von 12
# Laeufen nicht mehr auf, und die Aehnlichkeit stieg monoton bis 80. Preis:
# alle ~80 Zeichen eine Atempause -- im A/B-Hoervergleich als besser beurteilt.
MAX_SATZ_ZEICHEN = 80
# Die Zielgrenze ist weich: geschnitten wird nur an Klausel-Interpunktion.
# Ein Stueck OHNE solche Grenze bleibt bis hierher ganz -- ein Schnitt am
# blossen Leerzeichen mitten im Satzteil klingt wie ein Aussetzer. 250 ist
# die alte, fuer einzelne Saetze unauffaellige Obergrenze; das Verschlucken
# wurde nur bei Klauselketten weit darueber beobachtet.
MAX_SATZ_HART = 250


def split_sentences(text: str) -> list[str]:
    """Zerlegt in Saetze, damit der Worker Pausen dazwischen setzen kann.

    Sehr kurze Bruchstuecke werden bewusst wieder angehaengt: Phase 0 hat
    gemessen, dass dots.tts bei Fragmenten ohne Satzkontext halluziniert
    ("ähhh gemerdscht") und Inhalt verliert. Ganze Saetze sind unkritisch,
    Zwei-Wort-Schnipsel nicht.
    """
    teile = [teil.strip() for teil in _SATZENDE.split(text.strip()) if teil.strip()]
    if not teile:
        return []
    zusammengefasst: list[str] = []
    for teil in teile:
        if zusammengefasst and len(teil) < MIN_SATZ_ZEICHEN:
            zusammengefasst[-1] = f"{zusammengefasst[-1]} {teil}"
        else:
            zusammengefasst.append(teil)
    if len(zusammengefasst) > 1 and len(zusammengefasst[0]) < MIN_SATZ_ZEICHEN:
        zusammengefasst[1] = f"{zusammengefasst[0]} {zusammengefasst[1]}"
        zusammengefasst.pop(0)
    begrenzt: list[str] = []
    for teil in zusammengefasst:
        stuecke: list[str] = []
        while len(teil) > MAX_SATZ_ZEICHEN:
            schnitt = 0
            for treffer in re.finditer(r"[,;:–—](?=\s)", teil[:MAX_SATZ_ZEICHEN + 1]):
                if treffer.end() >= MIN_SATZ_ZEICHEN:
                    schnitt = treffer.end()
            if not schnitt:
                # Keine Klauselgrenze vor der Zielmarke: lieber ein laengeres
                # Stueck bis zur naechsten Grenze als ein Schnitt mitten im Satz.
                for treffer in re.finditer(r"[,;:–—](?=\s)", teil[:MAX_SATZ_HART + 1]):
                    if treffer.end() >= MIN_SATZ_ZEICHEN:
                        schnitt = treffer.end()
                        break
            if not schnitt:
                if len(teil) <= MAX_SATZ_HART:
                    break               # kein Schnittpunkt, aber tragbar: ganz lassen
                leerzeichen = [treffer.start() for treffer in
                               re.finditer(r"\s+", teil[:MAX_SATZ_HART + 1])
                               if treffer.start() >= MIN_SATZ_ZEICHEN]
                schnitt = leerzeichen[-1] if leerzeichen else MAX_SATZ_HART
            stuecke.append(teil[:schnitt].strip())
            teil = teil[schnitt:].strip()
        if teil:
            if stuecke and len(teil) < MIN_SATZ_ZEICHEN:
                stuecke[-1] = f"{stuecke[-1]} {teil}"
            else:
                stuecke.append(teil)
        begrenzt.extend(stuecke)
    return begrenzt


# Ab fuenf Grossbuchstaben. Laenge ist der einzige billige Trenner zwischen
# Wort und Akronym, und er ist nicht perfekt: GPU, JSON und VRAM bleiben
# richtig unangetastet, HTTPS faellt faelschlich mit durch. Wen das stoert,
# traegt das Wort in pronunciation.json ein -- die Tabelle laeuft vorher.
_VERSALWORT = re.compile(r"(?<![^\W\d_])[A-ZÄÖÜ]{5,}(?![^\W\d_])")


def entschaerfe_versalien(text: str) -> str:
    """VERSALIEN in Normalschreibung, sonst verhunzt dots.tts das Wort.

    Gemessen am 2026-08-10 mit der Stimme n0rd0m: "ANSWER:" kam als "Anna's
    door" heraus, "ANSWER." als "Anastbar", "Answer:" dagegen sauber. Das
    trifft jeden Text mit Versalien -- Nordoms Praefixe sind nur der Fall, an
    dem es auffiel.
    """
    return _VERSALWORT.sub(lambda treffer: treffer.group().capitalize(), text)


def endet_satz(teil: str) -> bool:
    """Endet das Stueck an einem Satzende -- oder mitten im Satz?

    split_sentences schneidet lange Saetze zusaetzlich an Komma und Semikolon
    (MAX_SATZ_ZEICHEN). An solchen Schnitten gehoert keine Atempause hin: der
    Satz laeuft ja weiter, und die Pause klingt dort wie ein Aussetzer.
    """
    return bool(_SATZENDE_AM_SCHLUSS.search(teil.rstrip()))


def default_voices_dir() -> Path:
    return Path(os.environ.get("MIMIC_VOICES_DIR", Path.home() / ".local/share/mimic/voices"))


def _regular_fd(fd: int, label: str, max_bytes: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise VoiceError("invalid_voice_profile", f"{label} ist keine regulaere Datei")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise VoiceError("invalid_voice_profile", f"{label} muss Modus 0600 haben")
    if info.st_size > max_bytes:
        raise VoiceError("invalid_voice_profile", f"{label} ist zu gross")
    return info


def _read_settings(profile_fd: int) -> tuple[str, float, str, dict]:
    """`settings.json` im Profil, optional. Fehlt sie, gelten die Vorgaben.

    Kein stiller Rueckfall bei kaputtem Inhalt: eine Stimme, die wegen eines
    Tippfehlers ploetzlich anders klingt, ist teurer zu finden als ein Fehler
    beim Laden.
    """
    try:
        fd = os.open("settings.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=profile_fd)
    except FileNotFoundError:
        return DEFAULT_SPRACHE, DEFAULT_SCALE, "", {}
    except OSError as exc:
        raise VoiceError("invalid_voice_profile",
                         f"settings.json ist unlesbar: {exc.strerror}") from None
    try:
        info = _regular_fd(fd, "settings.json", MAX_SETTINGS_BYTES)
        try:
            roh = json.loads(os.read(fd, info.st_size + 1).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise VoiceError("invalid_voice_profile", "settings.json ist kein gueltiges JSON")
    finally:
        os.close(fd)
    if not isinstance(roh, dict):
        raise VoiceError("invalid_voice_profile", "settings.json muss ein Objekt sein")
    sprache = roh.get("language", DEFAULT_SPRACHE)
    if sprache not in SPRACHEN:
        raise VoiceError("invalid_voice_profile",
                         f"language muss eines von {sorted(SPRACHEN)} sein")
    scale = roh.get("speaker_scale", DEFAULT_SCALE)
    # bool ist in Python ein int -- `true` waere sonst 1.0 und damit gueltig.
    if type(scale) not in (int, float) or not SCALE_MIN <= scale <= SCALE_MAX:
        raise VoiceError("invalid_voice_profile",
                         f"speaker_scale muss zwischen {SCALE_MIN} und {SCALE_MAX} liegen")
    effekt = roh.get("effekt", "")
    if effekt and not ist_effekt(effekt):
        raise VoiceError("invalid_voice_profile",
                         f"effekt muss eines von {sorted(EFFEKTE)} sein")
    # Die Klangregler gehoeren zur Stimme, nicht zum Auftrag: eine Stimme, die
    # nach GLaDOS klingen soll, tut das in jedem Text. Die Regler in der
    # Oberflaeche kommen oben drauf, sie ersetzen den Profilwert nicht.
    grenzen = {"tonhoehe": (TONHOEHE_MIN, TONHOEHE_MAX),
               "streuung": (STREUUNG_MIN, STREUUNG_MAX),
               "raster": (RASTER_MIN, RASTER_MAX),
               "formant": (FORMANT_MIN, FORMANT_MAX),
               "hall": (HALL_MIN, HALL_MAX),
               "verzerrung": (VERZERRUNG_MIN, VERZERRUNG_MAX),
               "kruemel": (KRUEMEL_MIN, KRUEMEL_MAX),
               "breite": (BREITE_MIN, BREITE_MAX)}
    regler = {}
    for name, (mini, maxi) in grenzen.items():
        wert = roh.get(name, 0.0)
        if type(wert) not in (int, float) or not mini <= wert <= maxi:
            raise VoiceError("invalid_voice_profile",
                             f"{name} muss zwischen {mini} und {maxi} liegen")
        regler[name] = float(wert)
    return sprache, float(scale), effekt, regler


def load_voice(name: str, voices_dir: Path | None = None, *,
               mit_gain: bool = True) -> VoiceProfile:
    """`mit_gain=False` fuer Aufrufer, die nur pruefen oder auflisten.

    Die Verstaerkung wird aus jeder Probe der Referenz gerechnet, ~22 ms je
    Stimme. Wer nur wissen will, ob ein Profil taugt, zahlt das umsonst: bei 27
    Profilen kostete `mimic voices` dadurch eine halbe Sekunde, und dieselbe
    Rechnung lief nochmal im Worker, weil der Cache prozesslokal ist.
    """
    if not isinstance(name, str) or not VOICE_RE.fullmatch(name):
        raise VoiceError("unknown_voice", "ungueltiger Stimmname")
    root = voices_dir or default_voices_dir()
    try:
        # O_NOFOLLOW auch hier: ohne das begann die Symlink-Sicherung erst
        # UNTERHALB des Stimmenverzeichnisses. Ein Symlink an `voices` selbst
        # haette auf ein fremdes Verzeichnis gezeigt, und ein Ziel mit Modus
        # 0700 besteht danach jede weitere Pruefung. Deckt die letzte
        # Pfadkomponente ab; symlinkende Elternverzeichnisse blieben ein Fall
        # fuer openat2(RESOLVE_NO_SYMLINKS) und sind hier nicht das Modell.
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        # Nicht nur FileNotFoundError: mit O_NOFOLLOW meldet ein Symlink an
        # dieser Stelle ENOTDIR, und ein roher OSError durch die Schnittstelle
        # waere fuer jeden Aufrufer ein Absturz statt einer Absage.
        raise VoiceError("unknown_voice", f"Stimme {name!r} existiert nicht") from None
    profile_fd = wav_fd = txt_fd = None
    try:
        if stat.S_IMODE(os.fstat(root_fd).st_mode) != 0o700:
            raise VoiceError("invalid_voice_profile", "Stimmverzeichnis muss Modus 0700 haben")
        try:
            profile_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=root_fd)
        except (FileNotFoundError, NotADirectoryError, OSError):
            raise VoiceError("unknown_voice", f"Stimme {name!r} existiert nicht") from None
        if stat.S_IMODE(os.fstat(profile_fd).st_mode) != 0o700:
            raise VoiceError("invalid_voice_profile", "Profilverzeichnis muss Modus 0700 haben")
        try:
            wav_fd = os.open("ref.wav", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=profile_fd)
            txt_fd = os.open("ref.txt", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=profile_fd)
        except OSError as exc:
            raise VoiceError("invalid_voice_profile", f"Profil unvollstaendig: {exc.strerror}") from None
        _regular_fd(wav_fd, "ref.wav", MAX_WAV_BYTES)
        text_info = _regular_fd(txt_fd, "ref.txt", MAX_TEXT_BYTES)
        try:
            prompt = os.read(txt_fd, text_info.st_size + 1).decode("utf-8").strip()
        except UnicodeDecodeError:
            raise VoiceError("invalid_voice_profile", "ref.txt ist kein UTF-8") from None
        if not prompt:
            raise VoiceError("invalid_voice_profile", "ref.txt ist leer")

        # /proc/self/fd haelt genau den bereits mit O_NOFOLLOW geoeffneten Inode fest;
        # ein nachtraeglicher Symlink-Tausch kann den Worker dadurch nicht umlenken.
        wav_path = f"/proc/self/fd/{wav_fd}"
        try:
            with wave.open(wav_path, "rb") as wav:
                channels, rate, frames = wav.getnchannels(), wav.getframerate(), wav.getnframes()
        except (wave.Error, EOFError, OSError) as exc:
            raise VoiceError("invalid_voice_profile", f"ref.wav ist unlesbar: {exc}") from None
        duration = frames / rate if rate else 0
        if channels != 1 or rate != 48_000:
            raise VoiceError("invalid_voice_profile", "ref.wav muss 48 kHz mono sein")
        if not 3 <= duration <= 60:
            raise VoiceError("invalid_voice_profile", "ref.wav muss 3 bis 60 Sekunden lang sein")
        # Der Pfad muss nach Rueckkehr noch existieren; dup uebernimmt die Lebenszeit.
        gain = _reference_gain(wav_path) if mit_gain else 1.0
        sprache, scale, effekt, regler = _read_settings(profile_fd)
        kept_fd = os.dup(wav_fd)
        return VoiceProfile(name, f"/proc/self/fd/{kept_fd}", prompt, gain, sprache, scale,
                            effekt, **regler)
    finally:
        for fd in (txt_fd, wav_fd, profile_fd, root_fd):
            if fd is not None:
                os.close(fd)


def close_voice(profile: VoiceProfile) -> None:
    try:
        os.close(int(profile.wav_path.rsplit("/", 1)[1]))
    except (ValueError, OSError):
        pass


def available_voices(voices_dir: Path | None = None) -> list[str]:
    """Nur Profile melden, die dieselbe Pruefung wie eine echte Anfrage bestehen."""
    root = voices_dir or default_voices_dir()
    try:
        names = sorted(entry.name for entry in root.iterdir() if VOICE_RE.fullmatch(entry.name))
    except OSError:
        return []
    valid: list[str] = []
    for name in names:
        # Ein blosses Verzeichnislisting waere ein falsches Bereitschaftssignal:
        # Rechte, Symlinks, WAV-Format und Dauer werden erst von load_voice geprueft.
        # Genau dieselbe Pruefung hier verhindert, dass /status eine Stimme verspricht,
        # die /speak unmittelbar danach ablehnen muss.
        try:
            profile = load_voice(name, root, mit_gain=False)
        except VoiceError:
            continue
        close_voice(profile)
        valid.append(name)
    return valid


def apply_pronunciation(text: str, path: Path | None = None) -> str:
    path = path or default_voices_dir().parent / "pronunciation.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return text
    if not isinstance(raw, dict):
        return text
    for source, replacement in raw.items():
        if (not isinstance(source, str) or not source or not isinstance(replacement, str)
                or not replacement):
            continue
        # Der Filter ist nur der zweite Riegel fuer Hub-freie Aufrufer: selbst
        # reine Woerter koennen den Sinn aendern. Das ist nicht theoretisch --
        # Kriterium B erkannte den Klon 12/12-mal an der Aussprache einzelner Woerter.
        # Darum schaltet der Hub-Pfad die Tabelle zusaetzlich vollstaendig aus.
        # Bindestrich ist erlaubt: er trennt Komposita, die dots.tts sonst
        # verschleift ("Satzende" wurde zu "satzende" statt "Satz-Ende"). Er
        # oeffnet keinen Pfad und keine URL -- Schraegstrich und Doppelpunkt
        # bleiben draussen.
        if not all(zeichen.isalpha() or zeichen in " -" for zeichen in source + replacement):
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(source)}(?!\w)", re.IGNORECASE)
        def replace(match: re.Match[str]) -> str:
            value = replacement
            return value[:1].upper() + value[1:] if match.group()[:1].isupper() else value
        text = pattern.sub(replace, text)
    return text


# ── Zahlen ──────────────────────────────────────────────────────────────
#
# dots.tts normalisiert Text nur fuer Chinesisch und Englisch
# (dots_tts/utils/text.py: detect_text_language liefert fuer Deutsch
# "unknown", normalize_text reicht dann roh durch). Ziffern kommen also
# unveraendert im Modell an, und es improvisiert: "1241" wurde am 2026-08-12
# als "flossweineinunswansich" gesprochen, "T-6.3" als "T sechsvudrai".
#
# Bewusst klein gehalten: ganze Zahlen, Dezimalzahlen, punktierte Kennungen.
# Keine Ordnungszahlen -- die haengen im Deutschen an Genus, Kasus und Numerus
# ("am dritten", "der dritte", "ein Drittel") und waeren ohne Wortartanalyse
# zwangslaeufig manchmal falsch. Lieber nichts als etwas Falsches.

_EINER = ("null", "eins", "zwei", "drei", "vier", "fuenf", "sechs", "sieben", "acht", "neun",
          "zehn", "elf", "zwoelf", "dreizehn", "vierzehn", "fuenfzehn", "sechzehn",
          "siebzehn", "achtzehn", "neunzehn")
_ZEHNER = ("", "", "zwanzig", "dreissig", "vierzig", "fuenfzig", "sechzig", "siebzig",
           "achtzig", "neunzig")


def _unter_hundert(zahl: int) -> str:
    if zahl < 20:
        return _EINER[zahl]
    zehner, einer = divmod(zahl, 10)
    if not einer:
        return _ZEHNER[zehner]
    # "einundzwanzig", nicht "einsundzwanzig" -- die Eins verliert ihr s.
    return f"{'ein' if einer == 1 else _EINER[einer]}und{_ZEHNER[zehner]}"


def _unter_tausend(zahl: int) -> str:
    hundert, rest = divmod(zahl, 100)
    if not hundert:
        return _unter_hundert(rest)
    kopf = f"{'ein' if hundert == 1 else _EINER[hundert]}hundert"
    return kopf if not rest else kopf + _unter_hundert(rest)


def zahlwort(zahl: int) -> str:
    """Eine ganze Zahl als deutsches Zahlwort. Bis knapp unter eine Milliarde."""
    if zahl < 0:
        return "minus " + zahlwort(-zahl)
    if zahl < 1000:
        return _unter_tausend(zahl)
    if zahl < 1_000_000:
        tausend, rest = divmod(zahl, 1000)
        kopf = f"{'ein' if tausend == 1 else _unter_tausend(tausend)}tausend"
        return kopf if not rest else kopf + _unter_tausend(rest)
    if zahl < 1_000_000_000:
        million, rest = divmod(zahl, 1_000_000)
        kopf = ("eine Million" if million == 1 else f"{_unter_tausend(million)} Millionen")
        return kopf if not rest else f"{kopf} {zahlwort(rest)}"
    # Darueber wird jedes Zahlwort laenger als der Satz. Ziffernweise ist dann
    # ehrlicher und bleibt verstaendlich.
    return " ".join(_EINER[int(ziffer)] for ziffer in str(zahl))


def _ziffernweise(text: str) -> str:
    return " ".join(_EINER[int(z)] if z.isdigit() else z for z in text)


# Kennung mit Punkten: T-6.3, v1.2.3, 6.3. Ziffernweise mit "punkt", weil ein
# Versionsstand keine Zahl ist -- "sechs punkt drei", nicht "dreiundsechzig".
_KENNUNG_PUNKT = re.compile(r"(?<![\w.,])([A-Za-z]{1,5}[-_]?)?(\d+(?:\.\d+)+)(?!\d)")
# Tausenderpunkte: 1.241, 12.000.000. Sieht aus wie eine Kennung, ist aber eine
# Zahl -- die Dreiergruppen verraten es.
_TAUSENDERPUNKT = re.compile(r"(?<![\w.,])\d{1,3}(?:\.\d{3})+(?!\d)")
_DEZIMAL = re.compile(r"(?<![\w.,])(\d+),(\d+)(?!\d)")
_GANZZAHL = re.compile(r"(?<![\w.,])\d+(?!\d|[.,]\d)")


def sprich_zahlen(text: str) -> str:
    """Ziffern zu deutschen Woertern, damit das Modell nicht raten muss.

    Reihenfolge ist wichtig: erst die Tausenderpunkte, sonst frisst sie die
    Kennungsregel; dann Kennungen, dann Dezimalzahlen, zuletzt der Rest.
    """
    text = _TAUSENDERPUNKT.sub(lambda t: zahlwort(int(t.group().replace(".", ""))), text)

    def kennung(treffer: re.Match) -> str:
        kopf = (treffer.group(1) or "").rstrip("-_")
        ziffern = " punkt ".join(_ziffernweise(teil) for teil in treffer.group(2).split("."))
        return f"{kopf} {ziffern}".strip()

    text = _KENNUNG_PUNKT.sub(kennung, text)
    text = _DEZIMAL.sub(lambda t: f"{zahlwort(int(t.group(1)))} komma {_ziffernweise(t.group(2))}",
                        text)
    return _GANZZAHL.sub(lambda t: zahlwort(int(t.group())), text)
