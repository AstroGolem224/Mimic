"""Sichere Stimmprofile und die kleine Aussprachekorrektur."""

from __future__ import annotations

import json
import os
import re
import stat
import wave
from dataclasses import dataclass
from pathlib import Path

VOICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_WAV_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024


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


def _reference_gain(wav_path: str) -> float:
    """Faktor, der die Referenz auf ZIEL_RMS_DBFS hebt."""
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
# 20, nicht 12. Gemessen am 2026-08-05: ein Satz mit 14 Zeichen kam in 4 von 22
# Generierungen ohne Sprache zurueck, Saetze mit 19 bis 21 Zeichen in 0 von 24.
# Der Worker faengt den Rest per Wiederholung ab (worker.STUMM_PEAK), aber die
# kostet dort hoerbare Totzeit -- billiger ist, den kurzen Satz an seinen
# Nachbarn zu haengen. Preis: an solchen Stellen entfaellt die Atempause.
MIN_SATZ_ZEICHEN = 20


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
    return zusammengefasst


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


def load_voice(name: str, voices_dir: Path | None = None) -> VoiceProfile:
    if not isinstance(name, str) or not VOICE_RE.fullmatch(name):
        raise VoiceError("unknown_voice", "ungueltiger Stimmname")
    root = voices_dir or default_voices_dir()
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError:
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
        gain = _reference_gain(wav_path)
        kept_fd = os.dup(wav_fd)
        return VoiceProfile(name, f"/proc/self/fd/{kept_fd}", prompt, gain)
    finally:
        for fd in (txt_fd, wav_fd, profile_fd, root_fd):
            if fd is not None:
                os.close(fd)


def close_voice(profile: VoiceProfile) -> None:
    try:
        os.close(int(profile.wav_path.rsplit("/", 1)[1]))
    except (ValueError, OSError):
        pass


def apply_pronunciation(text: str, path: Path | None = None) -> str:
    path = path or default_voices_dir().parent / "pronunciation.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return text
    if not isinstance(raw, dict):
        return text
    for source, replacement in raw.items():
        if not isinstance(source, str) or not source or not isinstance(replacement, str):
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(source)}(?!\w)", re.IGNORECASE)
        def replace(match: re.Match[str]) -> str:
            value = replacement
            return value[:1].upper() + value[1:] if match.group()[:1].isupper() else value
        text = pattern.sub(replace, text)
    return text
