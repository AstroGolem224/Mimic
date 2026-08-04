"""Kriterium A — laeuft dots.tts wirklich auf Blackwell (sm_120)?

Der Plan sagt ausdruecklich: `torch.cuda.is_available()` und die Arch-Liste sind
Vorpruefungen. Sie belegen, dass PyTorch sm_120 *anbietet*, nicht dass dots.tts'
Attention-Pfad, Vocoder und Codec dort *laufen*. Der bekannte Fehlermodus auf
Blackwell ist kein Absturz, sondern ein stiller CPU-Fallback, der nur dadurch
auffaellt, dass alles ~10x zu langsam ist.

Deshalb misst dieses Skript waehrend einer echten Generierung die GPU-Auslastung
in einem Hintergrund-Thread und faellt durch, wenn die GPU nicht gearbeitet hat.

Aufruf:  uv run python 00_blackwell.py
Exit 0 = Kriterium A bestanden.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import warnings

# Offline: die Checkpoints liegen nach fetch_checkpoints.py im Cache. Ein Griff
# ins Netz waere hier ein Befund, kein Komfort.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch  # noqa: E402
import yaml  # noqa: E402

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
CKPT = REVS["mf"]

# Ein Satz mit deutschen Umlauten und einem englischen Fachbegriff -- genug, um
# den Textpfad zu beruehren, kurz genug fuer einen Gate-Test.
TEXT = "Der Dienst laeuft lokal auf der Grafikkarte, komplett ohne Cloud."

# Schwellen. Bewusst grob: das hier trennt "GPU rechnet" von "CPU rechnet still",
# nicht gut von schlecht. Die Qualitaets- und Latenzzahlen kommen aus 02/03.
MIN_GPU_UTIL_PCT = 30
MIN_VRAM_DELTA_MIB = 1000
MAX_RTF = 1.0  # ueber Realtime = mit hoher Sicherheit CPU-Fallback


class GpuProbe(threading.Thread):
    """Pollt nvidia-smi, solange das Hauptthread-Modell rechnet."""

    def __init__(self, interval: float = 0.1) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self._halt = threading.Event()  # nicht _stop: kollidiert mit Thread._stop()
        self.max_util = 0
        self.max_mem = 0

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip().splitlines()[0]
                util, mem = (int(x) for x in out.split(","))
                self.max_util = max(self.max_util, util)
                self.max_mem = max(self.max_mem, mem)
            except Exception:
                pass
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2)


def vram_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[0]
    return int(out)


def main() -> int:
    fails: list[str] = []

    # -- Vorpruefungen (notwendig, nicht hinreichend) -----------------------
    cap = torch.cuda.get_device_capability(0) if torch.cuda.is_available() else None
    arches = torch.cuda.get_arch_list() if torch.cuda.is_available() else []
    print(f"torch            {torch.__version__}")
    print(f"cuda verfuegbar  {torch.cuda.is_available()}")
    print(f"device           {torch.cuda.get_device_name(0) if cap else '-'}")
    print(f"capability       {cap}")
    print(f"sm_120 in archs  {'sm_120' in arches}")

    if cap != (12, 0):
        fails.append(f"compute capability ist {cap}, erwartet (12, 0)")
    if "sm_120" not in arches:
        fails.append(f"sm_120 fehlt in torch.cuda.get_arch_list(): {arches}")
    if fails:
        for f in fails:
            print(f"FAIL  {f}")
        return 1

    # -- Echter End-to-End-Lauf --------------------------------------------
    from dots_tts.runtime import DotsTtsRuntime

    base_vram = vram_used_mib()
    print(f"\nVRAM vor Laden   {base_vram} MiB")

    t0 = time.perf_counter()
    rt = DotsTtsRuntime.from_pretrained(
        CKPT["repo"], revision=CKPT["revision"], precision="bfloat16",
    )
    load_s = time.perf_counter() - t0
    print(f"Ladezeit         {load_s:.1f} s")
    print(f"sample_rate      {rt.sample_rate} Hz")

    probe = GpuProbe()
    probe.start()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = rt.generate(text=TEXT, language="de")
    probe.stop()

    audio = out["audio"]
    dur_s = audio.shape[-1] / out["sample_rate"]
    vram_delta = probe.max_mem - base_vram

    print(f"\naudio            {dur_s:.2f} s, {tuple(audio.shape)}, device={audio.device}")
    print(f"rtf              {out['rtf']:.4f}  (elapsed {out['time_used']:.2f} s)")
    print(f"GPU-Auslastung   max {probe.max_util} %")
    print(f"VRAM-Peak        {probe.max_mem} MiB  (delta {vram_delta} MiB)")

    # -- Bewertung ----------------------------------------------------------
    if probe.max_util < MIN_GPU_UTIL_PCT:
        fails.append(f"GPU-Auslastung nur {probe.max_util}% < {MIN_GPU_UTIL_PCT}% "
                     "-- deutet auf CPU-Fallback")
    if vram_delta < MIN_VRAM_DELTA_MIB:
        fails.append(f"VRAM-Zuwachs nur {vram_delta} MiB < {MIN_VRAM_DELTA_MIB} MiB "
                     "-- Modell liegt vermutlich nicht auf der GPU")
    if out["rtf"] > MAX_RTF:
        fails.append(f"rtf {out['rtf']:.2f} > {MAX_RTF} -- langsamer als Realtime")
    if dur_s < 1.0:
        fails.append(f"Ausgabe nur {dur_s:.2f} s lang -- implausibel fuer den Testsatz")

    kernel_warnings = [
        str(w.message) for w in caught
        if any(s in str(w.message).lower()
               for s in ("no kernel image", "not compatible", "sm_", "fallback"))
    ]
    for w in kernel_warnings:
        fails.append(f"Kernel-Warnung: {w}")

    print()
    if fails:
        for f in fails:
            print(f"FAIL  {f}")
        return 1

    out_path = "out/00_blackwell.wav"
    os.makedirs("out", exist_ok=True)
    import soundfile as sf
    sf.write(out_path, audio.squeeze().float().cpu().numpy(), out["sample_rate"])
    print(f"PASS  Kriterium A. Probe geschrieben: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
