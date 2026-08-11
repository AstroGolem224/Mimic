"""Laedt MOSS-Modelle auf feste Revisionen, ohne dabei ueber die Falle zu stolpern.

Die Falle: `MossTTSProcessor.from_pretrained` reicht sein gesamtes `**kwargs` an
den Audio-Tokenizer weiter, der in einem *anderen* Repo liegt
(`OpenMOSS-Team/MOSS-Audio-Tokenizer`). Ein `revision=<sha des TTS-Repos>` trifft
dort auf nichts und endet in

    ValueError: Unrecognized model in OpenMOSS-Team/MOSS-Audio-Tokenizer.
    Should have a `model_type` key in its config.json

was wie ein kaputtes Repo aussieht, aber keins ist -- die config.json traegt
statt `model_type` eine `auto_map`, und die wird bei falscher Revision nie
gelesen.

Ausweg: beide Repos vorher auf ihre Revision herunterladen und dem Prozessor
nur noch lokale Pfade geben. Dann gibt es kein `revision` mehr, das
weitergereicht werden koennte, und die Pinnung ist trotzdem hart.
"""

from __future__ import annotations

import pathlib

import yaml
from huggingface_hub import snapshot_download

WURZEL = pathlib.Path(__file__).resolve().parent.parent
REVISIONEN = yaml.safe_load((WURZEL / "revisions.yaml").read_text())["checkpoints"]

# Eigene Eintraege, weil die Tokenizer eigene Repos mit eigener Historie sind.
# v1 gehoert zu VoiceGenerator (24 kHz mono), v2 zu Local-Transformer-v1.5
# (48 kHz stereo). Wer sie vertauscht, bekommt Rauschen, keinen Fehler.
TOKENIZER = {
    "v1": ("OpenMOSS-Team/MOSS-Audio-Tokenizer", "3cd226ba2947efa357ef453bcad111b6eafba782"),
    "v2": ("OpenMOSS-Team/MOSS-Audio-Tokenizer-v2", "f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169"),
}


def hole(schluessel: str) -> str:
    eintrag = REVISIONEN[schluessel]
    return snapshot_download(repo_id=eintrag["repo"], revision=eintrag["revision"])


def hole_tokenizer(fassung: str = "v1") -> str:
    repo, revision = TOKENIZER[fassung]
    return snapshot_download(repo_id=repo, revision=revision)
