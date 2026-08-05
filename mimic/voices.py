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
        kept_fd = os.dup(wav_fd)
        return VoiceProfile(name, f"/proc/self/fd/{kept_fd}", prompt)
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
