"""Stimmen aus einer Beschreibung entwerfen, statt sie einzusprechen.

Zwei Motoren zur Wahl, beide Apache-2.0, beide sprechen Deutsch **nativ**:

  voxcpm  openbmb/VoxCPM2, 2B, 48 kHz -- dieselbe Rate wie dots.tts, ein
          Entwurf ist also ohne Wandlung als ref.wav brauchbar. Die schoeneren
          Stimmen. Preis: die Aussprache sitzt nicht immer.
  qwen    Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign, 1.7B, 24 kHz. Fehlerfreies
          Deutsch im Hoervergleich vom 2026-08-12, dafuer halbe Bandbreite.

Kein Motor kann im Worker leben: beide bringen eigene torch- und
transformers-Pins mit, dots.tts haengt auf transformers 4.57.6. Deshalb je
eine eigene venv und ein Subprozess, der nur waehrend des Entwurfs existiert.
Das Modell belegt sein VRAM also nur dann, wenn wirklich entworfen wird -- ein
dauerhaft warmer zweiter Dienst waere fuer etwas, das man dreimal am Tag
klickt, der falsche Preis.

**Vorgaenger:** bis zum 2026-08-12 lief hier MOSS-VoiceGenerator, gefolgt von
einer Eindeutschungs-Stufe ueber MOSS-TTS-Local 4B. Beides ist entfernt.
MOSS-VoiceGenerator hat laut eigenem Paper nur Chinesisch und Englisch
gesehen; die entworfene Stimme war englisch konzipiert, und jeder folgende
Klonschritt hat den Akzent geerbt statt ihn zu tilgen. Ein Modell, das von
vornherein Deutsch kann, spart beide Stufen.

Die Umgebungen baut `mimic setup --entwurf`; sie liegen neben den Stimmen und
nicht im Repo, damit ein `git clean` sie nicht mitnimmt.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .voices import MAX_TEXT_BYTES

# Der cu128-Build ist auf PyPI nicht zu haben, und sm_120 braucht ihn.
TORCH = ["torch==2.9.1", "torchaudio==2.9.1"]
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"


@dataclass(frozen=True)
class Motor:
    """Ein Entwurfsmodell mit eigener Umgebung und eigenem Skript."""
    name: str
    anzeige: str
    skript: str
    rate: int
    hinweis: str
    pakete: list[str] = field(default_factory=list)


MOTOREN: dict[str, Motor] = {
    "voxcpm": Motor(
        name="voxcpm", anzeige="VoxCPM2", skript="entwerfen_voxcpm.py", rate=48_000,
        # Ohne Rate: die stellt die Oberflaeche aus `rate` selbst voran.
        hinweis="wie dots.tts, schoenere Stimmen, Aussprache streut",
        # xxhash<4: 4.0.0 bringt kein manylinux-Wheel fuer x86_64 mit, nur
        # aarch64, i686 und Windows -- ohne den Deckel bricht die Aufloesung ab.
        pakete=["voxcpm", "xxhash<4", "soundfile", "huggingface_hub"]),
    "qwen": Motor(
        name="qwen", anzeige="Qwen VoiceDesign", skript="entwerfen_qwen.py", rate=24_000,
        hinweis="fehlerfreies Deutsch, dafuer halbe Bandbreite als ref.wav",
        # Obergrenze 4.57.6 wie im Hauptprojekt: qwen-tts vertraegt die 5.x
        # nicht, die MOSS verlangt hatte.
        pakete=["qwen-tts>=0.0.5", "transformers>=4.36.0,<=4.57.6", "accelerate",
                "numpy>=1.26,<2.0", "numba>=0.60.0,<0.61.0", "librosa",
                "soundfile", "huggingface_hub"]),
}
VORGABE_MOTOR = "voxcpm"

# Deutsch, weil beide Motoren Deutsch koennen und der Probesatz woertlich das
# ref.txt des Profils wird. Rund zehn Sekunden, mit Aussage, Frage und Ausruf
# -- dieselbe Bauart wie die Referenztexte in charaktere.py und aus demselben
# Grund: dots.tts klont Prosodie mit, die Referenz muss die Intonationskurven
# also abdecken. Im Spike vom 2026-08-12 ergab er 9.4 bis 13.8 s.
STANDARDTEXT = ("Das Tor ist verriegelt, und hinter uns ist der Gang endlich still. "
                "Wie lange haben wir, bis es jemand merkt? "
                "Dann los, sofort, bevor das Licht zurueckkommt!")
# Englisch: beide Modelle verstehen die Beschreibung laut Doku auf Englisch,
# den Zieltext aber deutsch.
STANDARDBESCHREIBUNG = "A calm, low male voice, unhurried, warm but not soft."

MAX_KANDIDATEN = 4
# Wanduhrfrist fuer einen ganzen Auftrag. Vier Kandidaten mit je bis zu drei
# Wuerfen sind gemessen rund zwei Minuten; das Dreifache als Deckel faengt ein
# haengendes Modell ab, ohne einen langsamen Lauf zu erschlagen.
DECKEL_S = 600.0


def datenverzeichnis() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "mimic"


def motor_holen(name: str | None) -> Motor:
    motor = MOTOREN.get(name or VORGABE_MOTOR)
    if motor is None:
        raise ValueError(f"unbekannter Motor {name!r} -- {', '.join(sorted(MOTOREN))}")
    return motor


def venv_pfad(motor: str = VORGABE_MOTOR) -> Path:
    # Je Motor eine eigene Umgebung. Zusammenlegen ginge nicht: voxcpm und
    # qwen-tts bringen unvereinbare Pins mit, genau wie im Spike gemessen.
    return datenverzeichnis() / f"entwurf-venv-{motor}"


def python_pfad(motor: str = VORGABE_MOTOR) -> Path:
    return venv_pfad(motor) / "bin" / "python"


def umgebung_da(motor: str = VORGABE_MOTOR) -> bool:
    return python_pfad(motor).is_file()


def umgebungen_da() -> dict[str, bool]:
    return {name: umgebung_da(name) for name in MOTOREN}


def umgebung_bauen(motor: str = VORGABE_MOTOR, melden=print) -> None:
    """Legt die venv eines Motors an. Dauert Minuten und laedt einige GB."""
    eintrag = motor_holen(motor)
    if shutil.which("uv") is None:
        raise RuntimeError("uv fehlt -- ohne das kann die Umgebung nicht gebaut werden")
    ziel = venv_pfad(eintrag.name)
    melden(f"  {eintrag.anzeige} unter {ziel}")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    python = str(python_pfad(eintrag.name))
    schritte = [
        ["uv", "venv", "--python", "3.12", str(ziel)],
        # torch getrennt und mit eigenem Index, sonst kommt der CPU-Build.
        ["uv", "pip", "install", "--python", python, "--index-url", TORCH_INDEX, *TORCH],
        ["uv", "pip", "install", "--python", python, *eintrag.pakete],
    ]
    for schritt in schritte:
        melden(f"  {' '.join(schritt[:3])} ...")
        ergebnis = subprocess.run(schritt, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if ergebnis.returncode != 0:
            raise RuntimeError(f"{' '.join(schritt[:3])} fehlgeschlagen: "
                               f"{ergebnis.stderr.decode(errors='replace').strip()[:400]}")
    melden("  fertig -- das Modell selbst kommt beim ersten Entwurf")


def skript_pfad(motor: str = VORGABE_MOTOR) -> Path:
    return Path(__file__).resolve().parent / motor_holen(motor).skript


class Entwurf:
    """Ein Generatorlauf, gesteuert vom Fenster.

    Der Subprozess meldet je Ereignis eine JSON-Zeile; ein Lesefaden sammelt
    sie ein. Ohne den Faden wuerde die Pipe volllaufen und der Generator
    mittendrin blockieren.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.prozess: subprocess.Popen | None = None
        self.ordner: Path | None = None
        self.kandidaten: list[dict] = []
        self.ereignisse: list[dict] = []
        self.fehler = ""
        self.gestartet = 0.0
        self.beschreibung = ""
        self.text = ""
        self.motor = VORGABE_MOTOR

    def starten(self, beschreibung: str, text: str, anzahl: int,
                motor: str = VORGABE_MOTOR) -> None:
        beschreibung = " ".join(beschreibung.split())
        text = " ".join(text.split())
        eintrag = motor_holen(motor)          # ValueError bei unbekanntem Namen
        if not beschreibung:
            raise ValueError("Beschreibung fehlt")
        if not text:
            raise ValueError("Probesatz fehlt")
        if not 1 <= anzahl <= MAX_KANDIDATEN:
            raise ValueError(f"1 bis {MAX_KANDIDATEN} Kandidaten")
        # Vor dem GPU-Lauf pruefen, nicht danach: der Probesatz wird woertlich
        # das ref.txt, und load_voice lehnt mehr als MAX_TEXT_BYTES ab. Sonst
        # rechnet das Modell eine Minute, nur damit das Uebernehmen scheitert.
        if len(text.encode()) > MAX_TEXT_BYTES:
            raise ValueError("Probesatz ist zu lang fuer ein Stimmprofil")
        if len(beschreibung.encode()) > MAX_TEXT_BYTES:
            raise ValueError("Beschreibung ist zu lang")
        if not umgebung_da(eintrag.name):
            raise RuntimeError(f"Umgebung fuer {eintrag.anzeige} fehlt -- einmal "
                               f"`mimic setup --entwurf {eintrag.name}`")
        with self.lock:
            if self.prozess is not None:
                raise RuntimeError("es laeuft schon ein Entwurf")
            self._raeumen()
            ordner = datenverzeichnis() / "entwuerfe"
            shutil.rmtree(ordner, ignore_errors=True)
            ordner.mkdir(parents=True, exist_ok=True)
            ordner.chmod(0o700)
            auftrag = {"instruction": beschreibung, "text": text,
                       "anzahl": anzahl, "aus": str(ordner)}
            # stderr IN stdout, nicht in eine zweite Pipe. Sonst haengt der
            # Generator: transformers und tqdm schuetten ihre Fortschrittsbalken
            # nach stderr, ein Lesefaden bedient aber nur stdout -- nach 64 KB
            # ist die Pipe voll und das Kind blockiert in write(), fuer immer.
            # Gemessen am 2026-08-11: haengt reproduzierbar zwischen "laden" und
            # "bereit", sichtbar als wchan=anon_pipe_write.
            prozess = subprocess.Popen(
                [str(python_pfad(eintrag.name)), str(skript_pfad(eintrag.name)),
                 json.dumps(auftrag)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                # Eigene Prozessgruppe: terminate() traefe sonst nur den
                # Python-Prozess. Torch und die HF-Downloader starten Kinder,
                # die nach einem Abbruch weiterlebten und VRAM festhielten.
                start_new_session=True)
            self.prozess, self.ordner = prozess, ordner
            self.beschreibung, self.text = beschreibung, text
            self.motor = eintrag.name
            self.gestartet = time.monotonic()
            self.ereignisse = [{"kind": "start"}]
        threading.Thread(target=self._lesen, args=(prozess,), daemon=True).start()

    def _lesen(self, prozess: subprocess.Popen) -> None:
        geplapper: list[str] = []
        for zeile in prozess.stdout:
            try:
                ereignis = json.loads(zeile)
            except json.JSONDecodeError:
                # Fremdausgabe (tqdm, Warnungen) ist kein Ereignis -- aber die
                # letzten Zeilen davon sind der einzige Hinweis, wenn das Skript
                # stirbt, bevor es einen Fehler melden kann.
                if zeile.strip():
                    geplapper.append(zeile.strip())
                    del geplapper[:-5]
                continue
            with self.lock:
                self.ereignisse.append(ereignis)
                if ereignis.get("kind") == "kandidat":
                    self.kandidaten.append(ereignis)
                elif ereignis.get("kind") == "fehler":
                    self.fehler = str(ereignis.get("grund", ""))
        prozess.wait()
        # Die Pipe selbst schliessen: das Fenster lebt Stunden, und ein Entwurf
        # je Klick wuerde sonst Dateideskriptoren ansammeln.
        try:
            prozess.stdout.close()
        except OSError:
            pass
        with self.lock:
            # Nur den EIGENEN Lauf abraeumen. Abbrechen und sofort neu starten
            # laesst diesen Faden erst hier ankommen, wenn `starten` den Zustand
            # laengst dem neuen Lauf gegeben hat -- ohne die Wache loeschte der
            # Nachzuegler dessen Prozesszeiger, meldete seinen eigenen Abbruch
            # als dessen Fehler, und der naechste Start legte einen zweiten
            # Generator daneben.
            if self.prozess is not prozess:
                return
            if prozess.returncode != 0 and not self.fehler:
                # Etwa ein OOM-Kill oder ein fehlendes Paket: dann steht der
                # Grund nur im Geplapper.
                self.fehler = (" / ".join(geplapper))[-300:] or \
                    f"Abbruch mit Code {prozess.returncode}"
            self.prozess = None

    def _raeumen(self) -> None:
        self.kandidaten = []
        self.ereignisse = []
        self.fehler = ""

    def stand(self) -> dict:
        with self.lock:
            laeuft = self.prozess is not None
            sekunden = round(time.monotonic() - self.gestartet, 1) if self.gestartet else 0.0
            if laeuft and sekunden > DECKEL_S:
                self._toeten()
                self.fehler = f"Frist von {DECKEL_S:.0f} s gerissen"
                laeuft = False
            return {"laeuft": laeuft, "sekunden": sekunden if laeuft else 0.0,
                    "kandidaten": list(self.kandidaten), "fehler": self.fehler,
                    "beschreibung": self.beschreibung, "text": self.text,
                    "motor": self.motor,
                    "phase": self.ereignisse[-1].get("kind", "") if self.ereignisse else ""}

    def _toeten(self) -> None:
        prozess, self.prozess = self.prozess, None
        if prozess is None:
            return
        # Ganze Gruppe, nicht nur das Kind -- siehe start_new_session oben.
        # ProcessLookupError heisst: war schon tot, das ist kein Fehler.
        for signal_nummer in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(prozess.pid, signal_nummer)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                prozess.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue
        prozess.wait()

    def abbrechen(self) -> None:
        with self.lock:
            self._toeten()

    def datei(self, nummer: int) -> Path:
        with self.lock:
            for kandidat in self.kandidaten:
                if kandidat["nummer"] == nummer:
                    return Path(kandidat["datei"])
        raise KeyError(nummer)

    def schliessen(self) -> None:
        self.abbrechen()
        with self.lock:
            ordner, self.ordner = self.ordner, None
        if ordner is not None:
            shutil.rmtree(ordner, ignore_errors=True)


def _selbsttest() -> None:
    """Was ohne GPU pruefbar ist: die Torwaechter vor dem Subprozess."""
    entwurf = Entwurf()
    for beschreibung, text, anzahl, erwartet in [
        ("", "satz", 1, "Beschreibung"),
        ("  ", "satz", 1, "Beschreibung"),
        ("tief", "", 1, "Probesatz"),
        ("tief", "satz", 0, "Kandidaten"),
        ("tief", "satz", MAX_KANDIDATEN + 1, "Kandidaten"),
    ]:
        try:
            entwurf.starten(beschreibung, text, anzahl)
        except ValueError as fehler:
            assert erwartet in str(fehler), f"{beschreibung!r}: {fehler}"
        else:
            raise AssertionError(f"{beschreibung!r}/{anzahl} haette abgelehnt werden muessen")
    try:
        entwurf.starten("tief", "satz", 1, motor="gibtsnicht")
    except ValueError as fehler:
        assert "unbekannter Motor" in str(fehler), fehler
    else:
        raise AssertionError("unbekannter Motor haette abgelehnt werden muessen")
    stand = entwurf.stand()
    assert stand["laeuft"] is False and stand["kandidaten"] == [], stand
    for name in MOTOREN:
        assert skript_pfad(name).is_file(), f"{MOTOREN[name].skript} fehlt neben entwurf.py"
        assert "mimic" in str(venv_pfad(name)), venv_pfad(name)
    assert VORGABE_MOTOR in MOTOREN, VORGABE_MOTOR
    print("Selbsttest ok")


if __name__ == "__main__":
    _selbsttest()
