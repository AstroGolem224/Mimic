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

# Eigener Eintrag, weil der Tokenizer ein eigenes Repo mit eigener Historie ist.
TOKENIZER_REPO = "OpenMOSS-Team/MOSS-Audio-Tokenizer"
TOKENIZER_REVISION = "3cd226ba2947efa357ef453bcad111b6eafba782"


def hole(schluessel: str) -> str:
    eintrag = REVISIONEN[schluessel]
    return snapshot_download(repo_id=eintrag["repo"], revision=eintrag["revision"])


def hole_tokenizer() -> str:
    return snapshot_download(repo_id=TOKENIZER_REPO, revision=TOKENIZER_REVISION)
