"""Klont die Referenzen aus referenzen.yaml und spricht damit texte.yaml.

Aufrufweg aus chatterbox.mtl_tts nachgesehen, nicht geraten:
`ChatterboxMultilingualTTS.from_local(ckpt_dir, device)` und
`generate(text, language_id, audio_prompt_path, ...)`. Die Hyperparameter sind
die globalen Vorgaben aus voicebox (backends/chatterbox_backend.py:160-165) --
dort gibt es Sonderwerte nur fuer Hebraeisch, Deutsch faehrt die Vorgabe.

  uv run python 00_klonen.py                 # beide Referenzen, alle Texte
  uv run python 00_klonen.py matthias        # nur eine Referenz

Voraussetzung einmalig: `uv sync --python 3.12` und
`uv pip install --no-deps chatterbox-tts` -- warum, steht in pyproject.toml.
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
AUS = WURZEL / "out" / "klon" / "chatterbox"

SPRACHE = "de"
# Aus voicebox uebernommen. Nicht ohne Messung daran drehen -- niedrigere
# temperature und hoeheres cfg_weight machen die Sprache klarer, aber flacher.
HYPER = dict(exaggeration=0.5, cfg_weight=0.5, temperature=0.8, repetition_penalty=2.0)
SAAT = 20260811


def referenz_aufloesen(eintrag: dict) -> tuple[pathlib.Path, str]:
    """Pfad und woertlicher Referenztext, egal ob Mimic-Profil oder Entwurf."""
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


def main() -> None:
    nur = sys.argv[1] if len(sys.argv) > 1 else None
    referenzen = [r for r in REFERENZEN if nur is None or r["id"] == nur]
    if not referenzen:
        raise SystemExit(f"keine Referenz {nur!r} in referenzen.yaml")
    AUS.mkdir(parents=True, exist_ok=True)

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    if geraet != "cuda":
        print("WARNUNG: keine CUDA -- das dauert und Kriterium A gilt nicht", file=sys.stderr)

    from chatterbox.mtl_tts import ChatterboxMultilingualTTS, SUPPORTED_LANGUAGES

    if SPRACHE not in SUPPORTED_LANGUAGES:
        raise SystemExit(f"{SPRACHE!r} kann das Modell nicht: {sorted(SUPPORTED_LANGUAGES)}")

    # Erst auf die feste Revision herunterladen, dann von dort laden.
    # `from_pretrained` kennt kein revision-Argument und zoege sonst `main` --
    # dieselbe Falle wie bei MOSS, nur eine Ebene hoeher (siehe moss/laden.py).
    eintrag = REVISIONEN["chatterbox"]
    pfad = snapshot_download(repo_id=eintrag["repo"], revision=eintrag["revision"])
    begonnen = time.monotonic()
    modell = ChatterboxMultilingualTTS.from_local(pfad, device=geraet)
    print(f"geladen in {time.monotonic() - begonnen:.1f} s auf {geraet}", flush=True)

    for referenz in referenzen:
        ref_pfad, ref_text = referenz_aufloesen(referenz)
        print(f"\n== {referenz['id']} ({referenz['quelle']}) ==\n   {ref_text[:70]}...", flush=True)
        for eintrag_text in TEXTE:
            # Saat je Datei fest, damit ein Wiederholungslauf dieselbe Datei
            # ergibt. Ohne das ist ein Blindtest gegen ein bewegliches Ziel.
            torch.manual_seed(SAAT)
            begonnen = time.monotonic()
            wav = modell.generate(
                eintrag_text["text"],
                language_id=SPRACHE,
                audio_prompt_path=str(ref_pfad),
                **HYPER,
            )
            audio = wav.squeeze().cpu().numpy()
            ziel = AUS / f"{referenz['id']}_{eintrag_text['id']}.wav"
            sf.write(ziel, audio, modell.sr)
            dauer = len(audio) / modell.sr
            print(f"   {eintrag_text['id']:18s} {dauer:5.1f} s Audio in "
                  f"{time.monotonic() - begonnen:5.1f} s -> {ziel.name}", flush=True)


if __name__ == "__main__":
    main()
