"""Wie voxcpm/00_entwerfen.py, nur mit Qwen3-TTS-VoiceDesign.

Anderer Checkpoint als 00_klonen.py, dieselbe Bibliothek: VoiceDesign entwirft
aus einer Beschreibung (`instruct`) statt aus Referenzaudio zu klonen. Damit
gibt es keinen Weg, ueber den ein Akzent aus einer Referenz einsickern koennte
-- genau daran ist die MOSS-Kette gescheitert.

  uv run python 01_entwerfen.py              # alle vier Beschreibungen
  uv run python 01_entwerfen.py greis        # nur eine
"""

from __future__ import annotations

import pathlib
import sys
import time

import soundfile as sf
import torch
import yaml
from huggingface_hub import snapshot_download

WURZEL = pathlib.Path(__file__).resolve().parent.parent
REVISIONEN = yaml.safe_load((WURZEL / "revisions.yaml").read_text())["checkpoints"]
PLAN = yaml.safe_load((WURZEL / "entwuerfe.yaml").read_text())
AUS = WURZEL / "out" / "design" / "qwen"


def sprache_waehlen(modell) -> str:
    """Deutsch so nennen, wie das Modell es nennt. Wie in 00_klonen.py."""
    hole = getattr(modell.model, "get_supported_languages", None)
    unterstuetzt = list(hole()) if callable(hole) else []
    for kandidat in unterstuetzt:
        if str(kandidat).lower().startswith(("german", "de")):
            return kandidat
    raise SystemExit(f"kein Deutsch in {sorted(unterstuetzt)}")


def main() -> None:
    nur = sys.argv[1] if len(sys.argv) > 1 else None
    entwuerfe = [e for e in PLAN["entwuerfe"] if nur is None or e["id"] == nur]
    if not entwuerfe:
        raise SystemExit(f"kein Entwurf {nur!r} in entwuerfe.yaml")
    AUS.mkdir(parents=True, exist_ok=True)

    from qwen_tts import Qwen3TTSModel

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    eintrag = REVISIONEN["qwen_voicedesign"]
    pfad = snapshot_download(repo_id=eintrag["repo"], revision=eintrag["revision"])
    begonnen = time.monotonic()
    modell = Qwen3TTSModel.from_pretrained(pfad, device_map=geraet, torch_dtype=torch.bfloat16)
    print(f"geladen in {time.monotonic() - begonnen:.1f} s auf {geraet}", flush=True)
    sprache = sprache_waehlen(modell)
    print(f"Sprachschluessel: {sprache!r}", flush=True)

    for entwurf in entwuerfe:
        print(f"\n== {entwurf['id']} ({entwurf['rolle']}) ==", flush=True)
        for name, text in (("probesatz", PLAN["probesatz"]), ("prueftext", PLAN["prueftext"])):
            begonnen = time.monotonic()
            wellen, rate = modell.generate_voice_design(
                text=text, instruct=entwurf["beschreibung"], language=sprache)
            ziel = AUS / f"{entwurf['id']}_{name}.wav"
            sf.write(ziel, wellen[0], rate)
            print(f"   {name:10s} {len(wellen[0]) / rate:5.1f} s Audio in "
                  f"{time.monotonic() - begonnen:5.1f} s -> {ziel.name}", flush=True)


if __name__ == "__main__":
    main()
