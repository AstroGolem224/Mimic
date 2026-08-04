"""Schnellprobe: klingt der Klon nach dir?

Kein Abnahmekriterium, kein Messwert -- nur ein paar Saetze zum Reinhoeren,
bevor die richtigen Tests laufen. Nimmt die Referenz aus dem Stimmprofil.

Aufruf:  uv run python probe.py [--mf] ["eigener Satz" ...]
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import soundfile as sf  # noqa: E402
import yaml  # noqa: E402

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
STIMME = os.path.expanduser("~/.local/share/mimic/voices/matthias")
OUT = "out/probe"

STANDARD = [
    ("de", "Ich bin das hier nicht, aber ich klinge ziemlich genau so."),
    ("de", "Der Aufzug öffnet sich, und du entscheidest, ob du weitergehst."),
    ("de", "Ich habe den Pull Request gemerged, das Deployment hängt im Staging."),
    ("en", "This is not actually me, but it sounds a lot like me."),
    ("en", "After the final boss the elevator opens, and you decide what to do."),
]


def main(argv: list[str]) -> int:
    ckpt_name = "mf" if "--mf" in argv else "soar"
    eigene = [a for a in argv if not a.startswith("--")]
    # Alles faehrt das Sprach-Tag aus laden.SPRACH_TAG -- siehe dort.
    saetze = [(None, s) for s in eigene] if eigene else STANDARD

    ref_wav, ref_txt = f"{STIMME}/ref.wav", f"{STIMME}/ref.txt"
    for p in (ref_wav, ref_txt):
        if not os.path.exists(p):
            print(f"FEHLT: {p}\n  -> uv run python 02_aufnehmen.py referenz")
            return 2
    prompt_text = open(ref_txt).read().strip()
    info = sf.info(ref_wav)
    print(f"Referenz  {info.duration:.1f} s, {info.samplerate} Hz, {info.channels} ch")
    print(f"Transkript: {prompt_text[:70]}...")

    import laden
    t0 = time.perf_counter()
    rt = laden.runtime(ckpt_name)
    print(f"{ckpt_name} geladen in {time.perf_counter()-t0:.1f} s\n")

    os.makedirs(OUT, exist_ok=True)
    for i, (lang, text) in enumerate(saetze, 1):
        out = rt.generate(text=text, language=laden.SPRACH_TAG,
                          prompt_audio_path=ref_wav, prompt_text=prompt_text)
        pfad = f"{OUT}/{i:02d}_{lang}.wav"
        sf.write(pfad, out["audio"].squeeze().float().cpu().numpy(), out["sample_rate"])
        dauer = out["audio"].shape[-1] / out["sample_rate"]
        print(f"  {pfad}  {dauer:5.1f} s  rtf {out['rtf']:.3f}  [{lang}] {text[:48]}")

    print(f"\nAnhoeren:  pw-cat -p {OUT}/01_de.wav")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
