"""Lokale Transkription fuer hochgeladene Stimmreferenzen.

faster-whisper lebt absichtlich in einer eigenen Umgebung. CTranslate2 und
seine CUDA-Bibliotheken gehoeren weder in den dauerhaft laufenden TTS-Worker
noch in dessen ohnehin eng gepinnte torch/transformers-Umgebung.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


PAKET = "faster-whisper==1.2.1"
MODELL = os.environ.get("MIMIC_WHISPER_MODEL", "small")
DECKEL_S = 300


def datenverzeichnis() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "mimic"


def venv_pfad() -> Path:
    return datenverzeichnis() / "transkription-venv"


def python_pfad() -> Path:
    return venv_pfad() / "bin" / "python"


def skript_pfad() -> Path:
    return Path(__file__).resolve().parent / "transkribieren_whisper.py"


def umgebung_da() -> bool:
    return python_pfad().is_file()


def umgebung_bauen(melden=print) -> None:
    """Baut die kleine, vom TTS-Worker getrennte Whisper-Umgebung."""
    if shutil.which("uv") is None:
        raise RuntimeError("uv fehlt -- ohne das kann die Transkription nicht gebaut werden")
    ziel = venv_pfad()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    python = str(python_pfad())
    schritte = [
        ["uv", "venv", "--python", "3.12", str(ziel)],
        ["uv", "pip", "install", "--python", python, PAKET],
    ]
    melden(f"  faster-whisper unter {ziel}")
    for schritt in schritte:
        melden(f"  {' '.join(schritt[:3])} ...")
        ergebnis = subprocess.run(schritt, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if ergebnis.returncode != 0:
            raise RuntimeError(f"{' '.join(schritt[:3])} fehlgeschlagen: "
                               f"{ergebnis.stderr.decode(errors='replace').strip()[:400]}")
    melden("  fertig -- das Modell kommt beim ersten Transkribieren")


def transkribieren(quelle: Path) -> dict:
    """Transkribiert eine lokale WAV und gibt Text plus erkannte Sprache zurueck."""
    if not umgebung_da():
        raise RuntimeError("Transkription fehlt -- einmal `mimic setup --transkription`")
    auftrag = {"quelle": str(quelle), "modell": MODELL,
               # CPU ist der robuste Regelweg: CTranslate2 findet CUDA nur,
               # wenn auch seine eigenen cuBLAS/cuDNN-Laufzeitbibliotheken im
               # Suchpfad liegen. Fuer 3-60 s Referenz reicht int8 auf CPU.
               "geraet": os.environ.get("MIMIC_WHISPER_DEVICE", "cpu")}
    try:
        fertig = subprocess.run(
            [str(python_pfad()), str(skript_pfad()), json.dumps(auftrag)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=DECKEL_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Transkription nach {DECKEL_S} s abgebrochen") from None
    ereignis = None
    geplapper: list[str] = []
    for zeile in fertig.stdout.splitlines():
        try:
            wert = json.loads(zeile)
        except json.JSONDecodeError:
            if zeile.strip():
                geplapper.append(zeile.strip())
                del geplapper[:-5]
            continue
        if isinstance(wert, dict):
            ereignis = wert
    if ereignis is not None and ereignis.get("fehler"):
        raise RuntimeError(f"Transkription fehlgeschlagen: {ereignis['fehler']}")
    if fertig.returncode != 0 or ereignis is None:
        grund = " | ".join(geplapper) or f"Code {fertig.returncode}"
        raise RuntimeError(f"Transkription fehlgeschlagen: {grund}")
    text = " ".join(str(ereignis.get("text", "")).split())
    if not text:
        raise RuntimeError("Whisper hat keinen gesprochenen Text erkannt")
    return {"text": text, "sprache": str(ereignis.get("sprache", "")),
            "wahrscheinlichkeit": ereignis.get("wahrscheinlichkeit")}
