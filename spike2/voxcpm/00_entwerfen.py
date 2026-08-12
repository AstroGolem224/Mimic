"""Entwirft Stimmen mit VoxCPM2 und laesst sie DEUTSCH sprechen.

Der Unterschied zu moss/00_entwerfen.py ist der ganze Punkt dieses Laufs:
MOSS-VoiceGenerator hat laut eigenem Paper nur Chinesisch und Englisch
gesehen, VoxCPM2 nennt Deutsch unter 30 Sprachen und braucht keine
Sprachmarke -- der deutsche Text geht direkt hinein.

Aufrufweg aus der Modellkarte: die Beschreibung steht in Klammern VOR dem
Text, im selben String.

  uv run python 00_entwerfen.py              # alle vier Beschreibungen
  uv run python 00_entwerfen.py greis        # nur eine
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
AUS = WURZEL / "out" / "design" / "voxcpm"

# Aus der Modellkarte. cfg_value steuert, wie streng die Beschreibung befolgt
# wird; inference_timesteps ist die Zahl der Diffusionsschritte.
CFG = 2.0
SCHRITTE = 10


def main() -> None:
    nur = sys.argv[1] if len(sys.argv) > 1 else None
    entwuerfe = [e for e in PLAN["entwuerfe"] if nur is None or e["id"] == nur]
    if not entwuerfe:
        raise SystemExit(f"kein Entwurf {nur!r} in entwuerfe.yaml")
    AUS.mkdir(parents=True, exist_ok=True)

    from voxcpm import VoxCPM

    eintrag = REVISIONEN["voxcpm2"]
    pfad = snapshot_download(repo_id=eintrag["repo"], revision=eintrag["revision"])
    begonnen = time.monotonic()
    # optimize=False: das ist torch.compile. Phase 0 hat dafuer beim Kaltstart
    # von dots.tts 94 s statt 7 s gemessen -- fuer einen Hoervergleich ist die
    # Kompilierzeit teurer als die gesparte Rechenzeit.
    # load_denoiser=False: der Entrauscher arbeitet auf REFERENZaudio, und hier
    # gibt es keins.
    modell = VoxCPM.from_pretrained(pfad, load_denoiser=False, optimize=False,
                                    device="cuda" if torch.cuda.is_available() else "cpu")
    rate = modell.tts_model.sample_rate
    print(f"geladen in {time.monotonic() - begonnen:.1f} s, {rate} Hz", flush=True)

    for entwurf in entwuerfe:
        print(f"\n== {entwurf['id']} ({entwurf['rolle']}) ==", flush=True)
        for name, text in (("probesatz", PLAN["probesatz"]), ("prueftext", PLAN["prueftext"])):
            begonnen = time.monotonic()
            welle = modell.generate(text=f"({entwurf['beschreibung']}){text}",
                                    cfg_value=CFG, inference_timesteps=SCHRITTE)
            ziel = AUS / f"{entwurf['id']}_{name}.wav"
            sf.write(ziel, welle, rate)
            print(f"   {name:10s} {len(welle) / rate:5.1f} s Audio in "
                  f"{time.monotonic() - begonnen:5.1f} s -> {ziel.name}", flush=True)


if __name__ == "__main__":
    main()
