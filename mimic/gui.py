"""Fenster zum Vorlesen von Skripten mit mehreren Stimmen.

Format im Textfeld:

    #matthias_krieger: "Der Turm steht offen."
    #matthias_magier: Weisst du, was das bedeutet?
    Und das hier spricht weiter der Magier.

Die Anfuehrungszeichen sind optional. Eine Zeile ohne Praefix erbt den
Sprecher der Zeile darueber; ganz am Anfang gilt die links ausgewaehlte
Stimme. Aufeinanderfolgende Zeilen desselben Sprechers bilden einen Absatz;
eine Leerzeile trennt Einsaetze. Zeilen ab '//' werden uebersprungen.

Die Oberflaeche liegt als HTML in gui.html und laeuft in einem
Chromium-App-Fenster gegen einen kurzlebigen Loopback-Server. Grund: echtes
Glas (backdrop-filter), weiche Schatten und Rundungen gibt Tk nicht her, und
ein Toolkit mit Rendering-Faehigkeiten waere eine dreistellige
Megabyte-Abhaengigkeit fuer ein Fenster mit vier Knoepfen.
"""

from __future__ import annotations

import array
import functools
import hmac
import http.server
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .charaktere import CHARAKTERE
from .cli import (_dauer, open_request, profil_anlegen, profil_aus_datei, request,
                  speichern)
from .effekte import (breite_wert, formant_wert, hall_wert, kruemel_wert, raster_wert,
                      streuung_wert, tempo_faktor, tonhoehe_wert, verzerrung_wert)
from .entwurf import (MAX_KANDIDATEN, MOTOREN, STANDARDBESCHREIBUNG, STANDARDTEXT,
                      VORGABE_MOTOR, Entwurf, umgebungen_da)
from .protocol import MODES, read_frame
from .voices import (MAX_TEXT_BYTES, MAX_WAV_BYTES, VOICE_RE, VoiceError, close_voice,
                     default_voices_dir, load_voice)

SPRECHERPAUSE_MS = 300      # Luft zwischen zwei Einsaetzen
PEGEL_FENSTER = 600         # Proben je Balken im Wellenband, bei 24 kHz 40 Balken/s
STATUS_CACHE_S = 0.9        # /status des Dienstes nicht bei jedem Puls holen
STIMMEN_CACHE_S = 4.0       # Profilscan kostet je Stimme einen WAV-Kopf
MAX_TEXT_ZEICHEN = 100_000  # Grenze am Vertrauensrand; der Dienst prueft je Einsatz erneut
# Der Dienst nimmt 3-60 s. 8-15 s ist der einzige gemessene Bereich, siehe
# charaktere.py -- ausserhalb warnt die Oberflaeche, lehnt aber nicht ab.
DAUER_MIN_S, DAUER_MAX_S = 3.0, 60.0
DAUER_ZIEL = (8.0, 15.0)
AUFNAHME_DECKEL_S = 90.0    # Notbremse gegen eine vergessene laufende Aufnahme
AUFNEHMER = "pw-record"     # als Name gehalten, damit Tests ein Stubprogramm einhaengen koennen
# 192 kbps ist fuer eine Monospur reichlich und die Datei bleibt klein; die
# Zahl ist ueber die Umgebung stellbar, weil Geschmack hier streitbar ist.
MP3_BITRATE = os.environ.get("MIMIC_MP3_BITRATE", "192")
# lame zuerst: liest die WAV von stdin, schreibt MP3 nach stdout, ein Prozess.
# ffmpeg als Ausweichweg, weil es haeufiger installiert ist als lame.
MP3_KODIERER = (
    ("lame", ["--quiet", "-b", MP3_BITRATE, "-", "-"]),
    ("ffmpeg", ["-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                "-f", "mp3", "-b:a", f"{MP3_BITRATE}k", "pipe:1"]),
)
FORMATE = {"wav": ("audio/wav", "mimic.wav"), "mp3": ("audio/mpeg", "mimic.mp3")}


def stille(kopf: dict) -> bytes:
    return bytes(int(kopf["sample_rate"] * SPRECHERPAUSE_MS / 1000) * 2 * kopf["channels"])


@functools.lru_cache(maxsize=1)
def mp3_kodierer() -> tuple[str, list[str]] | None:
    """Einmal suchen: der Puls fragt das mehrmals je Sekunde ab."""
    for programm, argumente in MP3_KODIERER:
        pfad = shutil.which(programm)
        if pfad:
            return pfad, argumente
    return None


def nach_mp3(wav: bytes) -> bytes:
    """WAV nach MP3, ueber einen Fremdprozess -- Python kann kein MP3."""
    werkzeug = mp3_kodierer()
    if werkzeug is None:
        raise RuntimeError("kein MP3-Kodierer gefunden -- lame oder ffmpeg installieren")
    pfad, argumente = werkzeug
    fertig = subprocess.run([pfad, *argumente], input=wav,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if fertig.returncode != 0 or not fertig.stdout:
        grund = fertig.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"{Path(pfad).name} ist fehlgeschlagen: "
                           f"{grund[-1] if grund else f'Code {fertig.returncode}'}")
    return fertig.stdout


def pegel(pcm: bytes) -> list[float]:
    """Spitzenwert je Fenster, 0..1 -- Futter fuer das Wellenband."""
    proben = array.array("h")
    proben.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    werte = []
    for start in range(0, len(proben), PEGEL_FENSTER):
        fenster = proben[start:start + PEGEL_FENSTER]
        if not fenster:
            break
        werte.append(min(1.0, max(max(fenster), -min(fenster)) / 32767))
    return werte


@dataclass(frozen=True)
class Einsatz:
    stimme: str
    text: str


def parse_skript(quelle: str, standard: str) -> list[Einsatz]:
    """Zerlegt das Textfeld in Einsaetze. Reine Textverarbeitung, kein Netz."""
    einsaetze: list[Einsatz] = []
    aktuell = standard
    absatz: list[str] = []

    def abschliessen() -> None:
        if absatz:
            einsaetze.append(Einsatz(aktuell, " ".join(absatz)))
            absatz.clear()

    for zeile in quelle.splitlines():
        zeile = zeile.strip()
        if not zeile:
            abschliessen()
            continue
        if zeile.startswith("//"):
            continue
        if zeile.startswith("#") and ":" in zeile:
            kopf, rest = zeile[1:].split(":", 1)
            kopf = kopf.strip()
            if kopf:
                if kopf != aktuell:
                    abschliessen()
                aktuell = kopf
                zeile = rest.strip()
        if len(zeile) >= 2 and zeile[0] == zeile[-1] and zeile[0] in "\"'":
            zeile = zeile[1:-1].strip()
        if zeile:
            absatz.append(zeile)
    abschliessen()
    return einsaetze


def verfuegbare_stimmen() -> list[str]:
    root = default_voices_dir()
    namen = []
    try:
        eintraege = sorted(eintrag.name for eintrag in root.iterdir() if eintrag.is_dir())
    except FileNotFoundError:
        return []
    for name in eintraege:
        try:
            profil = load_voice(name, root, mit_gain=False)
        except VoiceError:
            continue
        close_voice(profil)
        namen.append(name)
    return namen


def stimmen_details() -> list[dict]:
    """Inventar fuer die Stimmenverwaltung -- auch kaputte Profile mit Grund."""
    root = default_voices_dir()
    try:
        eintraege = sorted(e.name for e in root.iterdir() if e.is_dir())
    except FileNotFoundError:
        return []
    inventar = []
    for name in eintraege:
        eintrag: dict = {"name": name, "ok": True, "grund": "", "dauer_s": None, "text": ""}
        try:
            profil = load_voice(name, root, mit_gain=False)
        except VoiceError as fehler:
            eintrag.update(ok=False, grund=f"{fehler.reason}: {fehler.message}")
        else:
            eintrag["text"] = profil.prompt_text
            close_voice(profil)
            try:
                eintrag["dauer_s"] = round(_dauer(root / name / "ref.wav"), 1)
            except (OSError, wave.Error):
                pass
        inventar.append(eintrag)
    return inventar


def dauer_urteil(dauer: float) -> tuple[bool, str]:
    if not DAUER_MIN_S <= dauer <= DAUER_MAX_S:
        return False, (f"{dauer:.1f} s liegt ausserhalb {DAUER_MIN_S:.0f}-{DAUER_MAX_S:.0f} s "
                       f"-- der Dienst wuerde das Profil ablehnen")
    if not DAUER_ZIEL[0] <= dauer <= DAUER_ZIEL[1]:
        return True, (f"{dauer:.1f} s -- brauchbar, aber ausserhalb der gemessenen "
                      f"{DAUER_ZIEL[0]:.0f}-{DAUER_ZIEL[1]:.0f} s")
    return True, f"{dauer:.1f} s -- im Zielbereich"


class Aufnahme:
    """pw-record ins Profilverzeichnis, gesteuert vom Fenster.

    Dieselbe Mechanik wie `mimic record`: SIGTERM an pw-record schliesst die
    WAV ordentlich ab, und die Datei liegt von Anfang an im Zielverzeichnis,
    weil os.replace keine Dateisystemgrenze ueberschreiten kann.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.prozess: subprocess.Popen | None = None
        self.name = ""
        self.profil: Path | None = None
        self.datei: Path | None = None
        self.gestartet = 0.0
        self.wache: threading.Timer | None = None
        self.letzte: dict | None = None     # fertige Aufnahme, wartet auf Behalten
        self.meldung: object | None = None  # stderr des Aufnehmers, erst nach dessen Ende gelesen
        self.abbruch = ""                   # Grund, falls der Aufnehmer von selbst ging

    def starten(self, name: str, force: bool) -> None:
        if not VOICE_RE.fullmatch(name):
            raise ValueError("Name nur a-z, 0-9, _ und -, max. 32 Zeichen, Anfang alphanumerisch")
        root = default_voices_dir()
        profil = root / name
        if (profil / "ref.wav").exists() and not force:
            raise ValueError(f"{name!r} existiert schon -- Ueberschreiben bestaetigen")
        with self.lock:
            if self.prozess is not None:
                raise RuntimeError("es laeuft schon eine Aufnahme")
            profil_anlegen(profil)
            self.name, self.profil = name, profil
            self.datei = profil / "ref.wav.tmp"
            self.letzte = None
            self.abbruch = ""
            self.gestartet = time.monotonic()
            # stderr in eine Datei statt in eine Pipe: sie laeuft nicht voll und
            # braucht keinen Leser-Thread. Ohne sie stirbt pw-record wortlos.
            if self.meldung is not None:
                self.meldung.close()
            self.meldung = tempfile.TemporaryFile()
            self.prozess = subprocess.Popen(
                [AUFNEHMER, "--rate", "48000", "--channels", "1", "--format", "s16",
                 str(self.datei)],
                stdout=subprocess.DEVNULL, stderr=self.meldung)
            self.wache = threading.Timer(AUFNAHME_DECKEL_S, self._deckel)
            self.wache.daemon = True
            self.wache.start()

    def _deckel(self) -> None:
        try:
            self.stoppen()
        except RuntimeError:
            pass

    def _meldung_text(self) -> str:
        """stderr des beendeten Aufnehmers. Nur nach dessen Ende aufrufen."""
        if self.meldung is None:
            return ""
        try:
            self.meldung.seek(0)
            return self.meldung.read().decode(errors="replace").strip()
        except (OSError, ValueError):
            return ""

    def _pruefe_tod(self) -> None:
        """Der Aufnehmer kann von selbst sterben -- ohne Geraet etwa sofort.

        Dann laeuft nur noch die Uhr im Fenster weiter und der Nutzer haelt
        eine Aufnahme fuer laufend, die es nie gab. Also hier abraeumen und
        den Grund merken, den pw-record nach stderr geschrieben hat.
        """
        with self.lock:
            prozess = self.prozess
            if prozess is None or prozess.poll() is None:
                return
            self.prozess = None
            if self.wache is not None:
                self.wache.cancel()
                self.wache = None
            grund = self._meldung_text() or "keine Meldung"
            self.abbruch = (f"{AUFNEHMER} endete von selbst (Code {prozess.returncode}): {grund}")
            datei, profil = self.datei, self.profil
            self.letzte = None
            self.datei = self.profil = None
        self._aufraeumen(datei, profil)

    @staticmethod
    def _aufraeumen(datei: Path | None, profil: Path | None) -> None:
        if datei is not None:
            datei.unlink(missing_ok=True)
        # Ein abgebrochener Versuch soll keine Bauruine hinterlassen. rmdir
        # raeumt nur das leere Verzeichnis ab -- ein Profil mit ref.wav bleibt.
        if profil is not None:
            try:
                profil.rmdir()
            except OSError:
                pass

    def stoppen(self) -> dict:
        with self.lock:
            prozess, datei = self.prozess, self.datei
            if prozess is None or datei is None:
                raise RuntimeError(self.abbruch or "es laeuft keine Aufnahme")
            self.prozess = None
            if self.wache is not None:
                self.wache.cancel()
                self.wache = None
        prozess.terminate()
        try:
            prozess.wait(timeout=5)
        except subprocess.TimeoutExpired:
            prozess.kill()
            prozess.wait()
        try:
            datei.chmod(0o600)
            dauer = _dauer(datei)
        except (OSError, wave.Error) as fehler:
            with self.lock:
                self.letzte = None
                grund = self._meldung_text()
            hinweis = f" -- {AUFNEHMER}: {grund}" if grund else ""
            raise RuntimeError(f"Aufnahme unbrauchbar: {fehler}{hinweis}") from None
        brauchbar, hinweis = dauer_urteil(dauer)
        ergebnis = {"name": self.name, "dauer_s": round(dauer, 1),
                    "brauchbar": brauchbar, "hinweis": hinweis}
        with self.lock:
            self.letzte = ergebnis
        return ergebnis

    def behalten(self, text: str) -> dict:
        text = " ".join(text.split())
        if not text:
            raise ValueError("Referenztext fehlt")
        if len(text.encode()) > MAX_TEXT_BYTES:
            raise ValueError("Referenztext ist zu lang")
        with self.lock:
            if self.letzte is None or self.profil is None or self.datei is None:
                raise RuntimeError("keine fertige Aufnahme vorhanden")
            if not self.letzte["brauchbar"]:
                raise ValueError(self.letzte["hinweis"])
            profil, datei, name = self.profil, self.datei, self.name
        speichern(profil, datei, text)
        try:
            geprueft = load_voice(name, mit_gain=False)
        except VoiceError as fehler:
            raise RuntimeError(f"{fehler.reason}: {fehler.message}") from None
        close_voice(geprueft)
        with self.lock:
            self.letzte = None
            self.datei = self.profil = None
        return {"name": name}

    def verwerfen(self) -> None:
        with self.lock:
            if self.prozess is not None:
                raise RuntimeError("erst die laufende Aufnahme stoppen")
            profil, datei = self.profil, self.datei
            self.letzte = None
            self.abbruch = ""
            self.datei = self.profil = None
        self._aufraeumen(datei, profil)

    def stand(self) -> dict:
        self._pruefe_tod()
        with self.lock:
            laeuft = self.prozess is not None
            return {"laeuft": laeuft, "name": self.name,
                    "sekunden": round(time.monotonic() - self.gestartet, 1) if laeuft else 0.0,
                    "deckel_s": AUFNAHME_DECKEL_S, "fertig": self.letzte,
                    "abbruch": self.abbruch}

    def schliessen(self) -> None:
        try:
            self.stoppen()
        except RuntimeError:
            return
        finally:
            if self.meldung is not None:
                self.meldung.close()
                self.meldung = None
        self.verwerfen()


def stimme_loeschen(name: str) -> None:
    if not VOICE_RE.fullmatch(name):
        raise ValueError("unbekannter Name")
    root = default_voices_dir()
    profil = (root / name).resolve()
    # Nach VOICE_RE kann name keinen Pfad enthalten; die Pruefung bleibt trotzdem,
    # weil hier unwiderruflich geloescht wird.
    if profil.parent != root.resolve() or not profil.is_dir():
        raise ValueError("kein Stimmprofil unter diesem Namen")
    for eintrag in sorted(profil.iterdir(), reverse=True):
        if eintrag.is_dir():
            raise ValueError("Profil enthaelt Unterverzeichnisse -- von Hand pruefen")
        eintrag.unlink()
    profil.rmdir()


class Abgebrochen(Exception):
    """Der Nutzer hat Stopp gedrueckt."""


def _socket_von(quelle) -> socket.socket | None:
    direkt = getattr(quelle, "sock", None)
    if direkt is not None:
        return direkt
    raw = getattr(getattr(quelle, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


class AktiveVerbindung:
    """Veroeffentlicht die GUI-Sitzung, bevor ein blockierendes Lesen beginnt."""

    def __init__(self, abbruch: threading.Event) -> None:
        self.abbruch = abbruch
        self.lock = threading.Lock()
        self.conn = None
        self.response = None

    def zuruecksetzen(self) -> None:
        with self.lock:
            self.conn = self.response = None

    def anfrage(self, body: dict):
        with self.lock:
            if self.abbruch.is_set():
                raise Abgebrochen
        def publish(conn) -> None:
            with self.lock:
                if self.abbruch.is_set():
                    conn.close()
                    raise Abgebrochen
                self.conn = conn
        try:
            conn = open_request("POST", "/speak", body, publish=publish,
                                cancelled=self.abbruch.is_set)
        except BaseException:
            with self.lock:
                conn = self.conn
                self.conn = None
            if conn is not None:
                conn.close()
            if self.abbruch.is_set():
                raise Abgebrochen from None
            raise
        try:
            response = conn.getresponse()
        except BaseException:
            if self.abbruch.is_set():
                raise Abgebrochen from None
            raise
        with self.lock:
            if self.conn is not conn or self.abbruch.is_set():
                response.close()
                conn.close()
                raise Abgebrochen
            self.response = response
        return response

    def schliessen(self) -> None:
        with self.lock:
            conn, response = self.conn, self.response
            self.conn = self.response = None
        if response is not None:
            response.close()
        if conn is not None:
            conn.close()

    def abbrechen(self) -> None:
        self.abbruch.set()
        with self.lock:
            conn, response = self.conn, self.response
            self.conn = self.response = None
        for quelle in (response, conn):
            sock = _socket_von(quelle)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if response is not None:
            response.close()
        if conn is not None:
            conn.close()


def sprich(einsatz: Einsatz, mode: str, senke, abbruch: threading.Event | None = None,
           aktive: AktiveVerbindung | None = None, regler: dict | None = None) -> int:
    """Holt einen Einsatz vom Dienst und schiebt jeden Block sofort in die Senke.

    Rueckgabe: wie viele Teilstuecke der Worker stumm aufgeben musste. Sie
    fehlen im Ton, ohne dass man es hoert -- deshalb gehoert die Zahl an die
    Oberflaeche und nicht nur ins Worker-Log.
    """
    body = {"text": einsatz.text, "voice": einsatz.stimme, "mode": mode, **(regler or {})}
    antwort = (aktive.anfrage(body) if aktive is not None
               else request("POST", "/speak", body))
    try:
        if antwort.status != 200:
            fehler = json.loads(antwort.read())
            raise RuntimeError(f"{fehler.get('reason')}: {fehler.get('message')}")
        art, nutzlast = read_frame(antwort)
        if art != "H":
            raise RuntimeError("Kopfrahmen fehlt")
        kopf = json.loads(nutzlast)
        while True:
            if abbruch is not None and abbruch.is_set():
                # Verbindung zumachen statt austrudeln lassen: der Worker sieht
                # den Abbruch am geschlossenen Socket und bricht die laufende
                # Erzeugung ab, statt sie fuer niemanden zu Ende zu rechnen.
                raise Abgebrochen
            art, nutzlast = read_frame(antwort)
            if art == "A":
                senke(kopf, nutzlast)
            elif art == "E":
                ende = json.loads(nutzlast)
                if ende.get("status") != "ok":
                    raise RuntimeError(f"{ende.get('reason')}: {ende.get('message')}")
                return int(ende.get("uebersprungen") or 0)
    finally:
        if aktive is not None:
            aktive.schliessen()
        else:
            verbindung = getattr(antwort, "_mimic_connection", None)
            if verbindung is not None:
                verbindung.close()


class Wiedergabe:
    """Ein pw-cat fuer das ganze Skript.

    Ein Prozess je Einsatz waere einfacher, kostet aber jedes Mal Startzeit
    und macht eine hoerbare Luecke zwischen den Sprechern. Der Rahmenkopf
    liefert die Rate; sie ist ueber alle Stimmen gleich, also reicht einer.
    """

    def __init__(self) -> None:
        self.prozess: subprocess.Popen | None = None
        self.kopf: dict | None = None

    def __call__(self, kopf: dict, pcm: bytes) -> None:
        if self.prozess is None:
            self.kopf = kopf
            self.prozess = subprocess.Popen(
                ["pw-cat", "--playback", "--raw", "--rate", str(kopf["sample_rate"]),
                 "--channels", str(kopf["channels"]), "--format", "s16", "-"],
                stdin=subprocess.PIPE, bufsize=0)
        assert self.prozess.stdin is not None
        self.prozess.stdin.write(pcm)

    def pause(self) -> None:
        if self.prozess is not None and self.kopf is not None:
            self(self.kopf, stille(self.kopf))

    def schliessen(self) -> None:
        if self.prozess is not None and self.prozess.stdin:
            self.prozess.stdin.close()
            self.prozess.wait()
            self.prozess = None

    def abbrechen(self) -> None:
        # Hartes Ende, kein geordnetes Schliessen: pw-cat wuerde seinen Puffer
        # sonst noch ausspielen, und Stopp soll sofort still sein.
        if self.prozess is not None:
            self.prozess.kill()
            self.prozess.wait()
            self.prozess = None


class Sammler:
    """Sammelt PCM fuer das Speichern; schreibt erst am Ende eine WAV."""

    def __init__(self) -> None:
        self.bloecke: list[bytes] = []
        self.kopf: dict | None = None

    def __call__(self, kopf: dict, pcm: bytes) -> None:
        self.kopf = kopf
        self.bloecke.append(pcm)

    def pause(self) -> None:
        if self.bloecke and self.kopf is not None:
            self.bloecke.append(stille(self.kopf))

    def _schreiben(self, ziel) -> None:
        if not self.bloecke or self.kopf is None:
            raise RuntimeError("nichts erzeugt")
        with wave.open(ziel, "wb") as ausgabe:
            ausgabe.setnchannels(self.kopf["channels"])
            ausgabe.setsampwidth(2)
            ausgabe.setframerate(self.kopf["sample_rate"])
            ausgabe.writeframes(b"".join(self.bloecke))

    def schreiben(self, ziel: Path) -> None:
        vorlaeufig = ziel.with_suffix(ziel.suffix + ".tmp")
        self._schreiben(str(vorlaeufig))
        vorlaeufig.replace(ziel)

    def wav(self) -> bytes:
        puffer = io.BytesIO()
        self._schreiben(puffer)
        return puffer.getvalue()


# ── GUI-Sitzung ─────────────────────────────────────────────────────────

class Sitzung:
    """Ein Auftrag zur Zeit, Zustand fuer den Puls der Oberflaeche."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        # Zweites, EINMALIGES Token nur fuer den ersten Seitenaufruf. Es steht
        # als ?t= in der Chromium-Kommandozeile, und /proc/<pid>/cmdline ist mit
        # Modus 0444 fuer jede fremde UID lesbar. Wer es dort spaeter abliest,
        # haelt einen verbrauchten Wert; das eigentliche `token` war nie in argv.
        self.start_token: str | None = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.abbruch = threading.Event()
        self.aktive = AktiveVerbindung(self.abbruch)
        self.thread: threading.Thread | None = None
        self.aufnahme = Aufnahme()
        self.entwurf = Entwurf()
        self.auftrag: dict = {"running": False, "index": 0, "total": 0, "voice": "",
                              "mode": "", "message": "", "ok": True, "download": False}
        self.pegel: list[float] = []
        self.export: dict | None = None     # {"daten", "typ", "name"} des letzten Auftrags
        self._status: tuple[float, dict] = (0.0, {})
        self._stimmen: tuple[float, list[str]] = (0.0, [])

    # -- Zwischenspeicher, damit der 220-ms-Puls den Dienst nicht schlaegt --

    def stimmen(self, frisch: bool = False) -> list[str]:
        alter, wert = self._stimmen
        if frisch or time.monotonic() - alter > STIMMEN_CACHE_S:
            wert = verfuegbare_stimmen()
            self._stimmen = (time.monotonic(), wert)
        return wert

    def dienst(self) -> dict:
        alter, wert = self._status
        if time.monotonic() - alter <= STATUS_CACHE_S:
            return wert
        try:
            antwort = request("GET", "/status")
            try:
                wert = json.loads(antwort.read()) if antwort.status == 200 else {}
            finally:
                antwort.close()
                verbindung = getattr(antwort, "_mimic_connection", None)
                if verbindung is not None:
                    verbindung.close()
        except (OSError, ValueError):
            wert = {"state": "offline"}
        self._status = (time.monotonic(), wert)
        return wert

    def melden(self, **felder) -> None:
        with self.lock:
            self.auftrag.update(felder)

    def starten(self, text: str, modus: str, standard: str, format: str | None,
                regler: dict | None = None) -> None:
        if self.aufnahme.stand()["laeuft"]:
            raise RuntimeError("es laeuft eine Aufnahme")
        # Der Entwurf haelt mehrere GB VRAM. Umgekehrt lehnt _entwerfen einen
        # Start waehrend eines Sprechauftrags schon ab -- ohne diese Zeile galt
        # die Ausschliesslichkeit nur in eine Richtung, und der Worker lief in
        # insufficient_vram oder riss dem Generator den Speicher weg.
        if self.entwurf.stand()["laeuft"]:
            raise RuntimeError("es laeuft ein Entwurf")
        with self.lock:
            if self.auftrag["running"]:
                raise RuntimeError("es laeuft noch ein Auftrag")
            self.abbruch.clear()
            self.aktive.zuruecksetzen()
            self.pegel = []
            self.export = None
            self.auftrag = {"running": True, "index": 0, "total": 0, "voice": "",
                            "mode": modus, "message": "wird vorbereitet…",
                            "ok": True, "download": False}
        self.thread = threading.Thread(target=self._lauf, name="mimic-gui-auftrag",
                                       args=(text, modus, standard, format, regler or {}),
                                       daemon=True)
        self.thread.start()

    def _lauf(self, text: str, modus: str, standard: str, format: str | None,
              regler: dict) -> None:
        senke = Sammler() if format else Wiedergabe()

        def gemessen(kopf: dict, pcm: bytes) -> None:
            senke(kopf, pcm)
            with self.lock:
                self.pegel.extend(pegel(pcm))

        try:
            einsaetze = parse_skript(text, standard)
            if not einsaetze:
                self.melden(running=False, message="nichts zu sprechen", ok=False)
                return
            unbekannt = {e.stimme for e in einsaetze} - set(self.stimmen(frisch=True))
            if unbekannt:
                self.melden(running=False, ok=False,
                            message="unbekannte Stimme: " + ", ".join(sorted(unbekannt)))
                return
            uebersprungen = 0
            for nummer, einsatz in enumerate(einsaetze, 1):
                self.melden(index=nummer, total=len(einsaetze), voice=einsatz.stimme,
                            message=f"spricht Einsatz {nummer} von {len(einsaetze)}")
                uebersprungen += sprich(einsatz, modus, gemessen, self.abbruch, self.aktive,
                                        regler)
                if nummer < len(einsaetze):
                    senke.pause()
            fehlend = (f" — {uebersprungen} Teilstueck(e) blieben stumm und fehlen"
                       if uebersprungen else "")
            if isinstance(senke, Sammler):
                daten = senke.wav()
                if format == "mp3":
                    self.melden(message="kodiert MP3…")
                    daten = nach_mp3(daten)
                typ, name = FORMATE[format or "wav"]
                with self.lock:
                    self.export = {"daten": daten, "typ": typ, "name": name}
                self.melden(running=False, download=True, ok=not uebersprungen,
                            message=f"{(format or 'wav').upper()} bereit "
                                    f"({len(daten) // 1024} KiB) — wird geladen{fehlend}")
            else:
                senke.schliessen()
                self.melden(running=False, ok=not uebersprungen,
                            message=f"{len(einsaetze)} Einsaetze gesprochen{fehlend}")
        except Abgebrochen:
            if isinstance(senke, Wiedergabe):
                senke.abbrechen()
            self.melden(running=False, ok=False, message="abgebrochen")
        except Exception as fehler:
            if isinstance(senke, Wiedergabe):
                senke.abbrechen()
            self.melden(running=False, ok=False, message=f"Fehler: {fehler}")

    def stoppen(self) -> None:
        self.aktive.abbrechen()

    def schliessen(self) -> None:
        self.stoppen()
        self.aufnahme.schliessen()
        self.entwurf.schliessen()
        if self.thread is not None:
            self.thread.join(2.0)


# ── Loopback-Server ─────────────────────────────────────────────────────

class _GuiHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MimicGui/1"
    sitzung: Sitzung        # von handler_klasse gesetzt

    def log_message(self, fmt: str, *args: object) -> None:
        pass                # ein Fenster, ein Nutzer -- Zugriffslog waere Rauschen

    # -- Antwortformen --

    def _senden(self, status: int, typ: str, koerper: bytes, kopf: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(koerper)))
        self.send_header("Cache-Control", "no-store")
        for name, wert in (kopf or {}).items():
            self.send_header(name, wert)
        self.end_headers()
        self.wfile.write(koerper)

    def _json(self, status: int, wert: dict) -> None:
        self._senden(status, "application/json",
                     json.dumps(wert, ensure_ascii=False).encode())

    def _keks(self) -> str:
        from http.cookies import SimpleCookie
        keks = SimpleCookie(self.headers.get("Cookie", ""))
        return keks["mimic_token"].value if "mimic_token" in keks else ""

    def _erlaubt(self, keks_reicht: bool = False) -> bool:
        """Token aus dem Kopf. Loopback allein reicht nicht: jeder andere
        Prozess des Nutzers koennte sonst den Dienst fernsteuern.

        Das Cookie allein reicht ebenfalls nicht -- der Browser haengt es an
        jede Anfrage, auch an eine, die eine fremde Seite ausloest. Der Kopf
        `X-Mimic-Token` ist das Gegenstueck: den kann nur Skript vom selben
        Ursprung setzen. Deshalb traegt das Cookie den Wert, und die Seite
        spiegelt ihn in den Kopf (Double Submit).

        `keks_reicht` gilt allein fuer GET, und GET liest hier nur. Ein
        `<audio src=...>` und ein Download ueber `location.href` kommen aus dem
        Browser selbst, nicht aus fetch() -- die koennen keinen Kopf setzen.
        Vorher trugen sie das Token als `?t=` in der URL; seit der Kopf Pflicht
        wurde, antworteten alle vier mit 403 (Wiedergabe der Referenz, der
        Aufnahme, der Entwuerfe und der Download-Ausweichweg). Fuer diese
        Ladewege haengt der Schutz an SameSite=Strict: eine fremde Seite darf
        das Cookie nicht mitschicken, auch nicht an ein eingebettetes <audio>.
        POST bleibt beim Kopf, dort sitzt jede Zustandsaenderung.
        """
        if hmac.compare_digest(self.headers.get("X-Mimic-Token", ""), self.sitzung.token):
            return True
        if keks_reicht and hmac.compare_digest(self._keks(), self.sitzung.token):
            return True
        self._json(403, {"message": "Token fehlt oder ist falsch"})
        return False

    def _seite_erlaubt(self) -> str | None:
        """Nur fuer GET / -- gibt das Cookie zurueck, das gesetzt werden muss.

        Zwei Wege hinein: das einmalige Start-Token aus der URL (erster Aufruf,
        danach verbraucht) oder das bereits gesetzte Cookie (Neuladen mit F5).
        """
        from urllib.parse import parse_qs, urlsplit

        if hmac.compare_digest(self._keks(), self.sitzung.token):
            return None                      # schon angemeldet, nichts zu setzen

        gegeben = parse_qs(urlsplit(self.path).query).get("t", [""])[0]
        with self.sitzung.lock:
            start = self.sitzung.start_token
            if start is not None and hmac.compare_digest(gegeben, start):
                self.sitzung.start_token = None      # einmalig, jetzt verbraucht
                return self.sitzung.token
        self._json(403, {"message": "Token fehlt oder ist falsch"})
        return ""

    # -- Felder aus dem JSON-Koerper, streng typisiert --
    #
    # `bool("false")` ist in Python True. Ein Koerper mit {"force": "false"}
    # haette also ein bestehendes Stimmprofil ueberschrieben -- genau das
    # Gegenteil dessen, was dort steht. Dasselbe Muster bei str() und int():
    # str({"a": 1}) ergibt klaglos einen Namen, int("3") eine Anzahl. Deshalb
    # wird der Typ geprueft statt umgebogen; ein falscher Typ ist ein
    # Nutzerfehler und bekommt 400.
    @staticmethod
    def _feld_text(wunsch: dict, name: str, vorgabe: str = "") -> str:
        wert = wunsch.get(name, vorgabe)
        if not isinstance(wert, str):
            raise ValueError(f"{name} muss Text sein")
        return wert

    @staticmethod
    def _feld_zahl(wunsch: dict, name: str, vorgabe: int) -> int:
        wert = wunsch.get(name, vorgabe)
        # bool ist in Python ein int -- True waere sonst die Anzahl 1.
        if type(wert) is not int:
            raise ValueError(f"{name} muss eine ganze Zahl sein")
        return wert

    @staticmethod
    def _feld_ja(wunsch: dict, name: str) -> bool:
        wert = wunsch.get(name, False)
        if type(wert) is not bool:
            raise ValueError(f"{name} muss true oder false sein")
        return wert

    def _koerper(self) -> dict:
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge <= 0 or laenge > MAX_TEXT_ZEICHEN * 4:
            return {}
        wert = json.loads(self.rfile.read(laenge))
        return wert if isinstance(wert, dict) else {}

    # -- Endpunkte --

    def do_GET(self) -> None:
        pfad = self.path.split("?", 1)[0]
        if pfad == "/":
            setzen = self._seite_erlaubt()
            if setzen == "":
                return
            kopf = {}
            if setzen:
                # SameSite=Strict: eine fremde Seite darf den Wert nicht
                # mitschicken. Kein HttpOnly -- die Seite muss ihn lesen, um ihn
                # in den X-Mimic-Token-Kopf zu spiegeln.
                kopf["Set-Cookie"] = f"mimic_token={setzen}; Path=/; SameSite=Strict"
            seite = (Path(__file__).parent / "gui.html").read_bytes()
            self._senden(200, "text/html; charset=utf-8", seite, kopf)
            return
        # GET liest hier ausnahmslos -- deshalb reicht das Cookie, siehe _erlaubt.
        if not self._erlaubt(keks_reicht=True):
            return
        if pfad == "/api/state":
            self._zustand()
        elif pfad == "/api/export":
            with self.sitzung.lock:
                fach = self.sitzung.export
            if fach is None:
                self._json(404, {"message": "keine Datei bereit"})
                return
            self._senden(200, fach["typ"], fach["daten"],
                         {"Content-Disposition": f'attachment; filename="{fach["name"]}"'})
        elif pfad == "/api/inventar":
            self._json(200, {"voices": stimmen_details(),
                             "charaktere": [{"name": name, "regie": wert.regie, "text": wert.text}
                                            for name, wert in sorted(CHARAKTERE.items())],
                             "dauer": {"min": DAUER_MIN_S, "max": DAUER_MAX_S,
                                       "ziel": list(DAUER_ZIEL)}})
        elif pfad == "/api/reference":
            self._referenz()
        elif pfad == "/api/take":
            self._take()
        elif pfad == "/api/design/state":
            bereit = umgebungen_da()
            self._json(200, {**self.sitzung.entwurf.stand(),
                             "umgebung": any(bereit.values()),
                             "motoren": [{"name": m.name, "anzeige": m.anzeige,
                                          "hinweis": m.hinweis, "rate": m.rate,
                                          "bereit": bereit[m.name]}
                                         for m in MOTOREN.values()],
                             "standard": {"beschreibung": STANDARDBESCHREIBUNG,
                                          "text": STANDARDTEXT, "max": MAX_KANDIDATEN,
                                          "motor": VORGABE_MOTOR}})
        elif pfad == "/api/design/audio":
            self._entwurf_audio()
        else:
            self._json(404, {"message": "unbekannter Endpunkt"})

    def _wav_datei(self, datei: Path, name: str) -> None:
        try:
            daten = datei.read_bytes()
        except OSError:
            self._json(404, {"message": "keine Aufnahme vorhanden"})
            return
        if len(daten) > MAX_WAV_BYTES:
            self._json(413, {"message": "Aufnahme ist zu gross"})
            return
        self._senden(200, "audio/wav", daten,
                     {"Content-Disposition": f'inline; filename="{name}"'})

    def _referenz(self) -> None:
        from urllib.parse import parse_qs, urlsplit
        name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
        if not VOICE_RE.fullmatch(name):
            self._json(400, {"message": "unbekannter Name"})
            return
        self._wav_datei(default_voices_dir() / name / "ref.wav", f"{name}.wav")

    def _take(self) -> None:
        aufnahme = self.sitzung.aufnahme
        with aufnahme.lock:
            datei = aufnahme.datei if aufnahme.letzte is not None else None
        if datei is None:
            self._json(404, {"message": "keine fertige Aufnahme"})
            return
        self._wav_datei(datei, "take.wav")

    def _entwurf_audio(self) -> None:
        from urllib.parse import parse_qs, urlsplit
        roh = parse_qs(urlsplit(self.path).query).get("nummer", [""])[0]
        try:
            datei = self.sitzung.entwurf.datei(int(roh))
        except (ValueError, KeyError):
            self._json(404, {"message": "kein solcher Kandidat"})
            return
        self._wav_datei(datei, f"entwurf_{roh}.wav")

    def do_POST(self) -> None:
        pfad = self.path.split("?", 1)[0]
        if not self._erlaubt():
            return
        try:
            wunsch = self._koerper()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json(400, {"message": "JSON ist ungueltig"})
            return
        if pfad == "/api/speak":
            self._sprechen(wunsch)
        elif pfad == "/api/stop":
            self.sitzung.stoppen()
            self._json(200, {"ok": True})
        elif pfad == "/api/warm":
            self._warm(wunsch)
        elif pfad.startswith("/api/record/") or pfad == "/api/voice/delete":
            self._profilpflege(pfad, wunsch)
        elif pfad.startswith("/api/design/"):
            self._entwerfen(pfad, wunsch)
        else:
            self._json(404, {"message": "unbekannter Endpunkt"})

    def _entwerfen(self, pfad: str, wunsch: dict) -> None:
        """Entwurfsreiter. Fehlerklassen wie bei _profilpflege."""
        entwurf = self.sitzung.entwurf
        try:
            if pfad == "/api/design/start":
                # Generator und Worker teilen sich die Karte. Beide gleichzeitig
                # rechnen zu lassen heisst, dass eins von beidem im VRAM
                # verhungert -- also erst den Sprechauftrag zu Ende.
                if self.sitzung.auftrag["running"]:
                    raise RuntimeError("es laeuft ein Sprechauftrag")
                entwurf.starten(self._feld_text(wunsch, "beschreibung"),
                                self._feld_text(wunsch, "text"),
                                self._feld_zahl(wunsch, "anzahl", 3),
                                self._feld_text(wunsch, "motor", VORGABE_MOTOR))
                self._json(200, {"ok": True, **entwurf.stand()})
            elif pfad == "/api/design/cancel":
                entwurf.abbrechen()
                self._json(200, {"ok": True})
            elif pfad == "/api/design/keep":
                self._entwurf_behalten(entwurf, wunsch)
            else:
                self._json(404, {"message": "unbekannter Endpunkt"})
                return
        except ValueError as fehler:
            self._json(400, {"message": str(fehler)})
            return
        except RuntimeError as fehler:
            self._json(409, {"message": str(fehler)})
            return
        except OSError as fehler:
            self._json(500, {"message": f"Dateisystem: {fehler}"})
            return
        self.sitzung.stimmen(frisch=True)

    def _entwurf_behalten(self, entwurf: Entwurf, wunsch: dict) -> None:
        try:
            datei = entwurf.datei(self._feld_zahl(wunsch, "nummer", -1))
        except (ValueError, TypeError, KeyError):
            raise ValueError("kein solcher Kandidat") from None
        stand = entwurf.stand()
        if stand["laeuft"]:
            raise RuntimeError("erst den laufenden Entwurf abwarten")
        # Der Probesatz wird woertlich das ref.txt: dots.tts bekommt Referenz
        # und Transkript als Paar, ein anderer Text dort macht den Klon kaputt.
        try:
            dauer, hinweis = profil_aus_datei(self._feld_text(wunsch, "name"), datei,
                                              stand["text"], self._feld_ja(wunsch, "force"))
        except VoiceError as fehler:
            code = 409 if "existiert schon" in fehler.message else 400
            self._json(code, {"message": fehler.message, "reason": fehler.reason})
            return
        self._json(200, {"ok": True, "dauer_s": round(dauer, 1), "hinweis": hinweis})

    def _profilpflege(self, pfad: str, wunsch: dict) -> None:
        """Aufnahme und Loeschen. ValueError = Nutzerfehler, RuntimeError = Ablauf."""
        aufnahme = self.sitzung.aufnahme
        try:
            if pfad == "/api/record/start":
                if self.sitzung.auftrag["running"]:
                    raise RuntimeError("es laeuft ein Sprechauftrag")
                aufnahme.starten(self._feld_text(wunsch, "name"), self._feld_ja(wunsch, "force"))
                self._json(200, {"ok": True, **aufnahme.stand()})
            elif pfad == "/api/record/stop":
                self._json(200, {"ok": True, **aufnahme.stoppen()})
            elif pfad == "/api/record/keep":
                self._json(200, {"ok": True, **aufnahme.behalten(self._feld_text(wunsch, "text"))})
            elif pfad == "/api/record/discard":
                aufnahme.verwerfen()
                self._json(200, {"ok": True})
            elif pfad == "/api/voice/delete":
                stimme_loeschen(self._feld_text(wunsch, "name"))
                self._json(200, {"ok": True})
            else:
                self._json(404, {"message": "unbekannter Endpunkt"})
                return
        except ValueError as fehler:
            self._json(400, {"message": str(fehler)})
            return
        except RuntimeError as fehler:
            self._json(409, {"message": str(fehler)})
            return
        except OSError as fehler:
            self._json(500, {"message": f"Dateisystem: {fehler}"})
            return
        self.sitzung.stimmen(frisch=True)      # Liste im Fenster sofort richtig

    def _zustand(self) -> None:
        from urllib.parse import parse_qs, urlsplit
        abfrage = parse_qs(urlsplit(self.path).query)
        try:
            seit = max(0, int(abfrage.get("seit", ["0"])[0]))
        except ValueError:
            seit = 0
        frisch = abfrage.get("frisch", ["0"])[0] == "1"
        with self.sitzung.lock:
            auftrag = dict(self.sitzung.auftrag)
            neue = self.sitzung.pegel[seit:]
            marke = len(self.sitzung.pegel)
            if auftrag["download"]:
                # Nur einmal ausliefern, sonst laedt jeder Puls die Datei erneut.
                self.sitzung.auftrag["download"] = False
        self._json(200, {"voices": self.sitzung.stimmen(frisch), "service": self.sitzung.dienst(),
                         "job": auftrag, "levels": neue, "cursor": marke,
                         "record": self.sitzung.aufnahme.stand(),
                         "mp3": mp3_kodierer() is not None})

    def _sprechen(self, wunsch: dict) -> None:
        text = wunsch.get("text")
        modus = wunsch.get("mode", "soar")
        stimme = wunsch.get("voice") or ""
        if not isinstance(text, str) or not text.strip():
            self._json(400, {"message": "text fehlt"})
            return
        if len(text) > MAX_TEXT_ZEICHEN:
            self._json(400, {"message": "Skript ist zu lang"})
            return
        if modus not in MODES:
            self._json(400, {"message": "mode muss mf, soar oder qwen sein"})
            return
        regler = {"tempo": tempo_faktor(wunsch.get("tempo")),
                  "tonhoehe": tonhoehe_wert(wunsch.get("tonhoehe")),
                  "streuung": streuung_wert(wunsch.get("streuung")),
                  "raster": raster_wert(wunsch.get("raster")),
                  "formant": formant_wert(wunsch.get("formant")),
                  "hall": hall_wert(wunsch.get("hall")),
                  "verzerrung": verzerrung_wert(wunsch.get("verzerrung")),
                  "kruemel": kruemel_wert(wunsch.get("kruemel")),
                  "breite": breite_wert(wunsch.get("breite"))}
        format = wunsch.get("format") or None
        if format is not None and format not in FORMATE:
            self._json(400, {"message": f"format muss {' oder '.join(FORMATE)} sein"})
            return
        if format == "mp3" and mp3_kodierer() is None:
            self._json(503, {"message": "kein MP3-Kodierer gefunden -- lame oder ffmpeg "
                                        "installieren"})
            return
        try:
            self.sitzung.starten(text, modus, stimme, format, regler)
        except RuntimeError as fehler:
            self._json(409, {"message": str(fehler)})
            return
        self._json(200, {"ok": True})

    def _warm(self, wunsch: dict) -> None:
        # Den Modus der Oberflaeche waermen, nicht einen festen. Fest "soar"
        # erzwang genau den Neustart, den der Warmlauf verhindern soll: das
        # Fenster spricht per Vorgabe mf, der Worker haette also erst soar
        # geladen und beim ersten Satz wieder abgeworfen (mode_restart).
        modus = wunsch.get("mode") or "mf"
        if modus not in MODES:
            self._json(400, {"message": "mode muss mf, soar oder qwen sein"})
            return
        try:
            antwort = request("POST", "/warm", {"mode": modus})
            try:
                wert = json.loads(antwort.read() or b"{}")
                status = antwort.status
            finally:
                antwort.close()
                verbindung = getattr(antwort, "_mimic_connection", None)
                if verbindung is not None:
                    verbindung.close()
        except (OSError, ValueError) as fehler:
            self._json(200, {"ok": False, "message": f"Dienst nicht erreichbar: {fehler}"})
            return
        self._json(200, {"ok": status < 400,
                         "message": wert.get("message") or wert.get("state") or f"HTTP {status}"})


def handler_klasse(sitzung: Sitzung) -> type[_GuiHandler]:
    return type("MimicGuiHandler", (_GuiHandler,), {"sitzung": sitzung})


# ── Fenster ─────────────────────────────────────────────────────────────

def _browser() -> str | None:
    for name in ("chromium", "google-chrome-stable", "google-chrome", "brave",
                 "brave-browser", "vivaldi-stable", "microsoft-edge-stable"):
        pfad = shutil.which(name)
        if pfad:
            return pfad
    return None


def _fenster(url: str) -> int:
    """Blockiert, bis das Fenster zu ist -- das ist unser Beenden-Signal."""
    programm = _browser()
    if programm is None:
        import webbrowser
        print(f"Kein Chromium gefunden. Oberflaeche im Standardbrowser:\n  {url}\n"
              f"Fenster schliessen und hier Strg+C druecken zum Beenden.")
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return 0
    # Frisches Profil je Lauf. Ohne --user-data-dir uebergibt Chromium die URL an
    # eine laufende Instanz und beendet sich sofort; ein FESTES Profil hat dieselbe
    # Luecke, sobald auf ihm noch ein Fenster von vorhin steht -- gemessen an einer
    # Instanz, die einen Tag lang offen war: der Aufruf kam sofort zurueck, das
    # finally in main() nahm den Server herunter, und das eben geoeffnete Fenster
    # zeigte ERR_CONNECTION_REFUSED. Ein eigenes Verzeichnis kann niemand belegen,
    # also blockiert der Aufruf wieder bis zum Schliessen. Das Profil traegt keinen
    # Zustand (die Oberflaeche benutzt weder localStorage noch Cookies) und faellt
    # deshalb am Ende weg statt sich im tmpfs zu stapeln -- rund 160 MB pro Fenster.
    laufzeit = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    laufzeit.mkdir(mode=0o700, parents=True, exist_ok=True)
    profil = Path(tempfile.mkdtemp(prefix="mimic-gui.", dir=laufzeit))
    try:
        return subprocess.run([programm, f"--app={url}", f"--user-data-dir={profil}",
                               "--window-size=1280,860", "--class=Mimic",
                               "--no-first-run", "--no-default-browser-check",
                               "--disable-features=Translate,MediaRouter"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    finally:
        # ponytail: nur das eigene Profil. Fremde mimic-gui.* aufzuraeumen wuerde
        # einem parallel laufenden Fenster den Boden wegziehen; was ein SIGKILL
        # liegen laesst, raeumt der naechste Neustart mit dem tmpfs ab.
        shutil.rmtree(profil, ignore_errors=True)


def main() -> int:
    sitzung = Sitzung()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_klasse(sitzung))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="mimic-gui-server", daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={sitzung.start_token}"
    try:
        return _fenster(url)
    finally:
        sitzung.schliessen()
        server.shutdown()
        server.server_close()


def demo() -> None:
    """Selbstpruefung ohne Fenster: Parser, Pegel und Token-Wache."""
    assert parse_skript('#a: "eins"\nzwei\n// weg\n', "z") == [
        Einsatz("a", "eins zwei")]
    assert parse_skript("Er sagte: komm.", "z") == [Einsatz("z", "Er sagte: komm.")]
    laut = array.array("h", [32767, -32768] * PEGEL_FENSTER).tobytes()
    assert pegel(laut) == [1.0, 1.0], pegel(laut)
    assert pegel(bytes(PEGEL_FENSTER * 2)) == [0.0]
    assert pegel(b"") == []

    assert dauer_urteil(2.9)[0] is False and dauer_urteil(60.1)[0] is False
    assert dauer_urteil(3.0)[0] and dauer_urteil(60.0)[0]
    assert "Zielbereich" in dauer_urteil(10.0)[1]
    assert "gemessenen" in dauer_urteil(30.0)[1]

    aufnahme = Aufnahme()
    assert aufnahme.stand() == {"laeuft": False, "name": "", "sekunden": 0.0,
                                "deckel_s": AUFNAHME_DECKEL_S, "fertig": None, "abbruch": ""}
    for boese in ("", "Gross", "../flucht", "_start", "x" * 33, "a/b"):
        try:
            aufnahme.starten(boese, False)
        except ValueError:
            pass
        else:                                   # pragma: no cover
            raise AssertionError(f"Name {boese!r} haette abgelehnt werden muessen")
    for boese in ("", "../../etc", "Gross"):
        try:
            stimme_loeschen(boese)
        except ValueError:
            pass
        else:                                   # pragma: no cover
            raise AssertionError(f"Loeschen von {boese!r} haette scheitern muessen")

    sitzung = Sitzung()
    assert len(sitzung.token) >= 24
    assert not sitzung.auftrag["running"]

    # Zwei Fensterlaeufe duerfen sich kein Profil teilen, sonst reicht Chromium
    # die URL an das alte Fenster weiter und beendet sich sofort.
    gesehen = []

    def _lauf_ohne_browser(befehl, **_rest):
        pfad = next(t.split("=", 1)[1] for t in befehl if t.startswith("--user-data-dir="))
        assert Path(pfad).is_dir(), pfad
        gesehen.append(pfad)
        return subprocess.CompletedProcess(befehl, 0)

    echter_browser, echter_lauf = globals()["_browser"], subprocess.run
    globals()["_browser"] = lambda: "/bin/true"
    subprocess.run = _lauf_ohne_browser
    try:
        assert _fenster("http://127.0.0.1:1/?t=x") == 0
        assert _fenster("http://127.0.0.1:1/?t=x") == 0
    finally:
        globals()["_browser"] = echter_browser
        subprocess.run = echter_lauf
    assert len(set(gesehen)) == 2, gesehen
    assert not [p for p in gesehen if Path(p).exists()], gesehen

    sammler = Sammler()
    # Eine halbe Sekunde Sinuston: Stille wuerde lame zu einem winzigen Rahmen
    # zusammenfalten und die Kodierung nicht wirklich pruefen.
    import math
    ton = array.array("h", [int(9000 * math.sin(i * 0.18)) for i in range(12000)])
    sammler({"sample_rate": 24000, "channels": 1}, ton.tobytes())
    wav = sammler.wav()
    assert wav[:4] == b"RIFF", wav[:4]

    if mp3_kodierer() is None:
        print("gui demo ok (ohne MP3: kein lame und kein ffmpeg)")
        return
    mp3 = nach_mp3(wav)
    # MPEG-Rahmen beginnt mit 11 gesetzten Bits, ein ID3-Kopf davor ist erlaubt.
    assert mp3[:3] == b"ID3" or (mp3[0] == 0xFF and mp3[1] & 0xE0 == 0xE0), mp3[:4]
    assert 0 < len(mp3) < len(wav), (len(mp3), len(wav))
    print(f"gui demo ok (MP3 {len(mp3)} von {len(wav)} Byte)")


if __name__ == "__main__":
    if os.environ.get("MIMIC_GUI_DEMO"):
        demo()
    else:
        raise SystemExit(main())
