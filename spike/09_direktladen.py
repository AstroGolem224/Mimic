"""Laden ohne den Umweg ueber 12 GB Systemspeicher.

Befund aus 08: `DotsTtsModel.from_pretrained` baut das Modell mit den
Torch-Defaults, also auf der CPU in fp32 -- bei ~2B Parametern rund 8 GB --
und laedt das State-Dict zusaetzlich nach CPU (`model.py:350`,
`load_file(path, device="cpu")`). Erst danach geht es auf die GPU und nach
bfloat16. Spitze gemessen: zwischen 10 und 12 GB RSS, Ruhezustand danach
4.4 GB. Auf einer 30-GiB-Maschine mit Desktop hat das den Kernel-OOM
ausgeloest.

Getestet werden drei Varianten, jede in einem eigenen Prozess mit hartem
Limit, damit "geht" und "geht nicht" nicht am Swap haengen:

  a_default     wie bisher
  b_dtype       Default-dtype bfloat16 -- halbiert die CPU-Materialisierung
  c_device      Default-Device cuda -- Konstruktion faellt direkt auf der GPU an

Geprueft wird nicht nur, ob es laedt, sondern auch, dass hinterher dasselbe
herauskommt: Ausgabelaenge und dass das Audio endlich ist.

Aufruf:  uv run python 09_direktladen.py a_default|b_dtype|c_device
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch  # noqa: E402
import yaml  # noqa: E402

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
TEXT = "Ein kurzer Satz, damit auch der Inferenzpfad Speicher sieht."


def proc_mib(feld: str) -> float:
    for zeile in open("/proc/self/status"):
        if zeile.startswith(feld):
            return int(zeile.split()[1]) / 1024
    return 0.0


def laden(variante: str):
    from dots_tts.runtime import DotsTtsRuntime
    c = REVS["mf"]
    bau = lambda: DotsTtsRuntime.from_pretrained(
        c["repo"], revision=c["revision"], precision="bfloat16", optimize=False)

    if variante == "a_default":
        return bau()
    if variante == "b_dtype":
        alt = torch.get_default_dtype()
        torch.set_default_dtype(torch.bfloat16)
        try:
            return bau()
        finally:
            torch.set_default_dtype(alt)
    if variante == "c_device":
        # Konstruktion direkt auf der GPU. Das State-Dict geht weiterhin ueber
        # CPU (upstream haelt device="cpu" fest), aber die fp32-Huelle auf der
        # CPU faellt weg -- und die ist der teure Teil.
        with torch.device("cuda"):
            return bau()
    raise SystemExit(f"unbekannte Variante: {variante}")


def main(variante: str) -> int:
    t0 = time.perf_counter()
    rt = laden(variante)
    dauer = time.perf_counter() - t0
    hwm_laden = proc_mib("VmHWM")

    out = rt.generate(text=TEXT, language="de")
    audio = out["audio"]
    dauer_audio = audio.shape[-1] / out["sample_rate"]

    print(f"{variante:12s}  HWM {proc_mib('VmHWM'):7.0f} MiB  "
          f"RSS {proc_mib('VmRSS'):6.0f} MiB  laden {dauer:5.1f} s  "
          f"audio {dauer_audio:4.1f} s  rtf {out['rtf']:.3f}")

    if not torch.isfinite(audio).all():
        print("  FAIL  Audio enthaelt NaN/Inf -- Variante unbrauchbar")
        return 1
    if dauer_audio < 1.0:
        print(f"  FAIL  Audio nur {dauer_audio:.1f} s -- implausibel")
        return 1

    import soundfile as sf
    os.makedirs("out/direktladen", exist_ok=True)
    pfad = f"out/direktladen/{variante}.wav"
    sf.write(pfad, audio.squeeze().float().cpu().numpy(), out["sample_rate"])
    print(f"  ok    {pfad}   (HWM beim Laden: {hwm_laden:.0f} MiB)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
