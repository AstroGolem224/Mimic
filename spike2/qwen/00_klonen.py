"""Wie chatterbox/00_klonen.py, nur mit Qwen3-TTS Base -- dieselben Referenzen,
dieselben Texte, dasselbe Ausgabeschema.

Base, nicht CustomVoice: nur der Base-Checkpoint klont aus Referenzaudio
(`create_voice_clone_prompt` / `generate_voice_clone`). CustomVoice hat neun
feste Sprecher und nimmt stattdessen eine `instruct`-Anweisung entgegen.

  uv run python 00_klonen.py                 # beide Referenzen, alle Texte
  uv run python 00_klonen.py matthias        # nur eine Referenz
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
REFERENZEN = yaml.safe_load((WURZEL / "referenzen.yaml").read_text())["referenzen"]
TEXTE = yaml.safe_load((WURZEL / "texte.yaml").read_text())["texte"]
AUS = WURZEL / "out" / "klon" / "qwen"

SAAT = 20260811


def referenz_aufloesen(eintrag: dict) -> tuple[pathlib.Path, str]:
    pfad = pathlib.Path(eintrag["pfad"]).expanduser()
    if not pfad.is_absolute():
        pfad = WURZEL / pfad
    if "text_datei" in eintrag:
        text = pathlib.Path(eintrag["text_datei"]).expanduser().read_text().strip()
    else:
        text = eintrag["text"]
    if not pfad.exists():
        raise SystemExit(f"Referenz {eintrag['id']}: {pfad} fehlt")
    return pfad, text


def sprache_waehlen(modell) -> str:
    """Deutsch so nennen, wie das Modell es nennt -- statt zu raten.

    qwen_tts prueft gegen `get_supported_languages()` und wirft sonst einen
    ValueError. Die Schreibweise ("german", "German", "de") ist nicht
    dokumentiert, also wird sie erfragt.
    """
    hole = getattr(modell.model, "get_supported_languages", None)
    unterstuetzt = list(hole()) if callable(hole) else []
    for kandidat in unterstuetzt:
        if str(kandidat).lower().startswith(("german", "de")):
            return kandidat
    raise SystemExit(f"kein Deutsch in {sorted(unterstuetzt)}")


def main() -> None:
    nur = sys.argv[1] if len(sys.argv) > 1 else None
    referenzen = [r for r in REFERENZEN if nur is None or r["id"] == nur]
    if not referenzen:
        raise SystemExit(f"keine Referenz {nur!r} in referenzen.yaml")
    AUS.mkdir(parents=True, exist_ok=True)

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    if geraet != "cuda":
        print("WARNUNG: keine CUDA -- das dauert und Kriterium A gilt nicht", file=sys.stderr)

    from qwen_tts import Qwen3TTSModel

    eintrag = REVISIONEN["qwen"]
    pfad = snapshot_download(repo_id=eintrag["repo"], revision=eintrag["revision"])
    begonnen = time.monotonic()
    # bf16 direkt aufs Geraet. Phase 0 hat gemessen, dass der Umweg ueber fp32
    # auf der CPU 12.5 GB RAM zieht und den Prozess killen kann -- RAM ist der
    # Engpass, nicht VRAM (README, Abschnitt Messwerte).
    modell = Qwen3TTSModel.from_pretrained(pfad, device_map=geraet, torch_dtype=torch.bfloat16)
    print(f"geladen in {time.monotonic() - begonnen:.1f} s auf {geraet}", flush=True)
    sprache = sprache_waehlen(modell)
    print(f"Sprachschluessel: {sprache!r}", flush=True)

    for referenz in referenzen:
        ref_pfad, ref_text = referenz_aufloesen(referenz)
        print(f"\n== {referenz['id']} ({referenz['quelle']}) ==\n   {ref_text[:70]}...", flush=True)
        # Der Klon-Prompt haengt nur an der Referenz, nicht am Text -- einmal
        # je Stimme statt einmal je Satz.
        prompt = modell.create_voice_clone_prompt(
            ref_audio=str(ref_pfad), ref_text=ref_text, x_vector_only_mode=False
        )
        for eintrag_text in TEXTE:
            torch.manual_seed(SAAT)
            begonnen = time.monotonic()
            wavs, rate = modell.generate_voice_clone(
                text=eintrag_text["text"], voice_clone_prompt=prompt, language=sprache
            )
            audio = wavs[0]
            ziel = AUS / f"{referenz['id']}_{eintrag_text['id']}.wav"
            sf.write(ziel, audio, rate)
            print(f"   {eintrag_text['id']:18s} {len(audio) / rate:5.1f} s Audio in "
                  f"{time.monotonic() - begonnen:5.1f} s -> {ziel.name}", flush=True)


if __name__ == "__main__":
    main()
