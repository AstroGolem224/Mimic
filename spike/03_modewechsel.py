"""Schritt 7 — koennen `mf` und `soar` sich einen Prozess teilen?

Das ist laut Plan Voraussetzung fuer Phase 1, nicht Neugier: davon haengt ab,
ob P1-5 ("ein Eigentuemer je Modell") einen billigen Modus-Parameter meint oder
einen Prozesswechsel.

Prueft zusaetzlich die Annahme aus P1-3 gegen genau diese Last: gibt `del` +
`empty_cache()` den VRAM zurueck, oder braucht es wirklich das Prozessende?
dAImon hat das fuer whisper gemessen (T-1.2), nicht fuer dots.tts.

Aufruf:  uv run python 03_modewechsel.py
"""

from __future__ import annotations

import gc
import os
import subprocess
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import torch  # noqa: E402
import yaml  # noqa: E402

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
TEXT = "Kurzer Satz zur Kontrolle, dass das Modell nach dem Wechsel noch spricht."


def vram() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True).stdout.strip().splitlines()[0]
    return int(out)


def lade(name: str):
    from dots_tts.runtime import DotsTtsRuntime
    c = REVS[name]
    t0 = time.perf_counter()
    rt = DotsTtsRuntime.from_pretrained(
        c["repo"], revision=c["revision"], precision="bfloat16", optimize=False)
    return rt, time.perf_counter() - t0


def spricht(rt) -> float:
    out = rt.generate(text=TEXT, language="de")
    return out["audio"].shape[-1] / out["sample_rate"]


def main() -> int:
    basis = vram()
    print(f"VRAM Basis                    {basis:6d} MiB\n")

    rt_mf, t_mf = lade("mf")
    v_mf = vram()
    d_mf = spricht(rt_mf)
    print(f"mf geladen      {t_mf:5.1f} s   {v_mf:6d} MiB  (+{v_mf-basis})  spricht {d_mf:.1f} s")

    # -- Beide gleichzeitig? -------------------------------------------------
    rt_soar, t_soar = lade("soar")
    v_beide = vram()
    print(f"soar dazu       {t_soar:5.1f} s   {v_beide:6d} MiB  (+{v_beide-v_mf})")

    d_soar = spricht(rt_soar)
    d_mf2 = spricht(rt_mf)
    print(f"beide sprechen nach dem Laden des zweiten: soar {d_soar:.1f} s, mf {d_mf2:.1f} s")
    v_peak = vram()
    print(f"VRAM Peak beide resident      {v_peak:6d} MiB  (+{v_peak-basis} ueber Basis)\n")

    # -- Gibt Entladen im Prozess den Speicher zurueck? ----------------------
    del rt_soar
    gc.collect()
    torch.cuda.empty_cache()
    time.sleep(2)
    v_nach_del = vram()
    zurueck = v_peak - v_nach_del
    print(f"nach del+empty_cache(soar)    {v_nach_del:6d} MiB  (zurueck: {zurueck} MiB)")
    print(f"noch belegt ueber Basis       {v_nach_del-basis:6d} MiB")

    print("\n-- Befund " + "-" * 62)
    passt = v_peak - basis < 30000
    print(f"beide resident moeglich:   {'JA' if passt else 'NEIN'} "
          f"({(v_peak-basis)/1024:.1f} GiB von 32 GiB)")
    entladen_taugt = zurueck > 4000
    print(f"del+empty_cache gibt frei: {'JA' if entladen_taugt else 'NEIN'} "
          f"({zurueck} MiB von ~6000 erwartet)")
    if not entladen_taugt:
        print("  -> P1-3 bestaetigt: Residenz gehoert in einen Prozess, der sich beendet.")
    else:
        print("  -> P1-3 fuer diese Last widerlegt; Prozessende trotzdem die robustere Zusage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
