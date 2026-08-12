"""Stimmen aus einer Beschreibung entwerfen, statt sie einzusprechen.

Der Generator (MOSS-VoiceGenerator) kann nicht im Worker leben: er verlangt
transformers 5.0.0, dots.tts haengt auf 4.57.6. Deshalb eine eigene venv und
ein Subprozess, der nur waehrend des Entwurfs existiert. Das Modell belegt
seine ~6 GB VRAM also nur dann, wenn wirklich entworfen wird -- ein zweiter
dauerhaft warmer Dienst waere fuer etwas, das man dreimal am Tag klickt, der
falsche Preis.

Die Umgebung baut `mimic setup --entwurf`; sie liegt neben den Stimmen und
nicht im Repo, damit ein `git clean` sie nicht mitnimmt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Aus spike2/moss/pyproject.toml uebernommen, wo diese Pins gemessen sind.
# transformers 5.0.0 exakt: mit 5.15.0 scheitert das Laden des Audio-Tokenizers
# an einer config.json, die nur eine auto_map traegt und kein `model_type`.
TORCH = ["torch==2.9.1", "torchaudio==2.9.1"]
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
PAKETE = ["transformers==5.0.0", "accelerate", "soundfile", "librosa", "huggingface_hub"]

# Rund zehn Sekunden, mit Aussage, Frage und Ausruf -- dieselbe Bauart wie die
# Referenztexte in charaktere.py, und aus demselben Grund: dots.tts klont
# Prosodie mit, die Referenz muss die Intonationskurven also abdecken.
# Englisch, weil MOSS-VoiceGenerator laut Modellkarte Chinesisch und Englisch
# kann. Deutsch steht dort nicht.
STANDARDTEXT = ("The gate is sealed and the corridor behind us is quiet. "
                "How long do you think we have before they notice? "
                "Then move, now, before the lights come back!")
STANDARDBESCHREIBUNG = "A calm, low male voice, unhurried, warm but not soft."

MAX_KANDIDATEN = 4
# Wanduhrfrist fuer einen ganzen Auftrag. Vier Kandidaten mit je bis zu drei
# Wuerfen sind gemessen rund zwei Minuten; das Dreifache als Deckel faengt ein
# haengendes Modell ab, ohne einen langsamen Lauf zu erschlagen.
DECKEL_S = 600.0


def datenverzeichnis() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "mimic"


def venv_pfad() -> Path:
    return datenverzeichnis() / "entwurf-venv"


def python_pfad() -> Path:
    return venv_pfad() / "bin" / "python"


def umgebung_da() -> bool:
    return python_pfad().is_file()


def umgebung_bauen(melden=print) -> None:
    """Legt die Generator-venv an. Dauert Minuten und laedt einige GB."""
    if shutil.which("uv") is None:
        raise RuntimeError("uv fehlt -- ohne das kann die Umgebung nicht gebaut werden")
    ziel = venv_pfad()
    melden(f"  Umgebung unter {ziel}")
    ziel.parent.mkdir(parents=True, exist_ok=True)
    schritte = [
        ["uv", "venv", "--python", "3.12", str(ziel)],
        # torch getrennt und mit eigenem Index: der cu128-Build ist auf PyPI
        # nicht zu haben, und sm_120 braucht ihn.
        ["uv", "pip", "install", "--python", str(python_pfad()),
         "--index-url", TORCH_INDEX, *TORCH],
        ["uv", "pip", "install", "--python", str(python_pfad()), *PAKETE],
    ]
    for schritt in schritte:
        melden(f"  {' '.join(schritt[:3])} ...")
        ergebnis = subprocess.run(schritt, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if ergebnis.returncode != 0:
            raise RuntimeError(f"{' '.join(schritt[:3])} fehlgeschlagen: "
                               f"{ergebnis.stderr.decode(errors='replace').strip()[:400]}")
    melden("  fertig -- das Modell selbst (~4 GB) kommt beim ersten Entwurf")


def skript_pfad() -> Path:
    return Path(__file__).resolve().parent / "entwerfen.py"


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

    def starten(self, beschreibung: str, text: str, anzahl: int) -> None:
        beschreibung = " ".join(beschreibung.split())
        text = " ".join(text.split())
        if not beschreibung:
            raise ValueError("Beschreibung fehlt")
        if not text:
            raise ValueError("Probesatz fehlt")
        if not 1 <= anzahl <= MAX_KANDIDATEN:
            raise ValueError(f"1 bis {MAX_KANDIDATEN} Kandidaten")
        if not umgebung_da():
            raise RuntimeError("Generator-Umgebung fehlt -- einmal `mimic setup --entwurf`")
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
                [str(python_pfad()), str(skript_pfad()), json.dumps(auftrag)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            self.prozess, self.ordner = prozess, ordner
            self.beschreibung, self.text = beschreibung, text
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
                    "phase": self.ereignisse[-1].get("kind", "") if self.ereignisse else ""}

    def _toeten(self) -> None:
        prozess, self.prozess = self.prozess, None
        if prozess is None:
            return
        prozess.terminate()
        try:
            prozess.wait(timeout=5)
        except subprocess.TimeoutExpired:
            prozess.kill()
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
    stand = entwurf.stand()
    assert stand["laeuft"] is False and stand["kandidaten"] == [], stand
    assert skript_pfad().is_file(), "entwerfen.py liegt nicht neben entwurf.py"
    assert "mimic" in str(venv_pfad()), venv_pfad()
    print("Selbsttest ok")


if __name__ == "__main__":
    _selbsttest()
