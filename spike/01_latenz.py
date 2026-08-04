"""Kriterium C — TTFA p95 < 300 ms, und die Zahlen fuer E und die Sperrfrist.

Misst drei Dinge getrennt, weil sie im Plan getrennt bewertet werden:

  TTFA        Zeit bis zum ersten Audio-Chunk, Modell bereits geladen und warm.
              Das ist die Zahl fuer Kriterium C.
  Kaltstart   Prozessstart + Checkpoint-Laden + Warmlauf bis zur ersten warm
              bedienten Anfrage. Zahl fuer Kriterium E (< 60 s) und fuer den
              Vergleich gegen dAImons GPU_FRIST_S = 120.0.
  RTF         Verhaeltnis Rechenzeit zu erzeugter Audiodauer, als Gegenprobe
              zur README-Angabe.

Messpunkt: der Rueckgabezeitpunkt des ersten Chunks aus `generate_stream`.
Das ist NICHT der im Plan geforderte client-seitige Punkt hinter einem Socket --
den gibt es in Phase 0 noch nicht. Die Differenz ist Socket- und
Prozessgrenzen-Overhead und geht zulasten des Dienstes, nicht zugunsten:
die hier gemessene Zahl ist eine untere Schranke.

Aufruf:
  uv run python 01_latenz.py                 # warm, n=50, optimize=True
  uv run python 01_latenz.py --no-optimize   # Gegenprobe ohne Kompilierung
  uv run python 01_latenz.py --kaltstart     # nur Kriterium E, einmalig
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import yaml  # noqa: E402

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
CORPUS = yaml.safe_load(open("corpus.yaml"))

# Feste Saat: dieselbe Reihenfolge bei jedem Lauf, damit zwei Messungen
# vergleichbar sind. Randomisiert gegenueber der Korpus-Reihenfolge, nicht
# gegenueber vorherigen Laeufen.
SEED = 20260804
TTFA_BUDGET_S = 0.300
KALTSTART_BUDGET_S = 60.0
GPU_FRIST_S = 120.0  # daimon/hub/daemon.py:89


def saetze() -> list[tuple[str, str, str]]:
    """(id, sprache, text) fuer alle DE- und EN-Saetze des Korpus."""
    out = []
    for lang in ("de", "en"):
        for e in CORPUS[lang]:
            out.append((e["id"], lang, e["text"]))
    return out


def perzentil(werte: list[float], p: float) -> float:
    """Lineare Interpolation, wie numpy.percentile default. Explizit benannt,
    weil der Plan die Methode mitberichtet haben will."""
    s = sorted(werte)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def lade(optimize: bool):
    import laden
    return laden.runtime("mf", optimize=optimize)


def ein_ttfa(rt, text: str, lang: str) -> tuple[float, float, float]:
    """(ttfa_s, gesamt_s, audio_s) fuer eine Aeusserung."""
    t0 = time.perf_counter()
    ttfa = None
    samples = 0
    for chunk in rt.generate_stream(text=text, language=lang):
        if ttfa is None:
            ttfa = time.perf_counter() - t0
        samples += chunk.shape[-1]
    gesamt = time.perf_counter() - t0
    return ttfa, gesamt, samples / rt.sample_rate


def kaltstart(optimize: bool) -> None:
    """Kriterium E: von null bis zur ersten warm bedienten Anfrage."""
    t0 = time.perf_counter()
    rt = lade(optimize)
    geladen = time.perf_counter() - t0
    sid, lang, text = saetze()[0]
    ttfa, _, _ = ein_ttfa(rt, text, lang)
    bereit = time.perf_counter() - t0

    print(f"\n-- Kaltstart (optimize={optimize}) " + "-" * 30)
    print(f"Laden bis bereit      {geladen:6.1f} s")
    print(f"bis erstes Audio      {bereit:6.1f} s")
    print(f"Kriterium E (< 60 s)  {'PASS' if bereit < KALTSTART_BUDGET_S else 'FAIL'}")
    marge = GPU_FRIST_S / geladen if geladen else float("inf")
    print(f"gegen GPU_FRIST_S=120 {geladen:.1f} s Ladezeit, Faktor {marge:.1f}x Marge "
          f"{'(ok)' if marge >= 3 else '(knapp -- Frist im dAImon-Task pruefen)'}")


def messreihe(n: int, optimize: bool) -> None:
    rt = lade(optimize)
    alle = saetze()
    rng = random.Random(SEED)
    plan = [alle[rng.randrange(len(alle))] for _ in range(n + 3)]

    print(f"\n-- Warmlauf (3 Laeufe, verworfen) " + "-" * 24)
    for sid, lang, text in plan[:3]:
        ein_ttfa(rt, text, lang)

    print(f"-- Messreihe n={n}, optimize={optimize} " + "-" * 22)
    roh = []
    for i, (sid, lang, text) in enumerate(plan[3:], 1):
        ttfa, gesamt, audio_s = ein_ttfa(rt, text, lang)
        roh.append({"i": i, "id": sid, "lang": lang, "ttfa_s": ttfa,
                    "gesamt_s": gesamt, "audio_s": audio_s,
                    "rtf": gesamt / audio_s if audio_s else None})
        if i % 10 == 0:
            print(f"  {i}/{n}")

    ttfas = [r["ttfa_s"] for r in roh]
    rtfs = [r["rtf"] for r in roh if r["rtf"]]
    p95 = perzentil(ttfas, 0.95)

    print(f"\nTTFA  min {min(ttfas)*1000:6.1f} ms")
    print(f"      med {statistics.median(ttfas)*1000:6.1f} ms")
    print(f"      p95 {p95*1000:6.1f} ms   (lineare Interpolation, n={len(ttfas)})")
    print(f"      max {max(ttfas)*1000:6.1f} ms")
    print(f"RTF   med {statistics.median(rtfs):.4f}   (README mf: 0.15)")
    print(f"\nKriterium C (p95 < 300 ms)  {'PASS' if p95 < TTFA_BUDGET_S else 'FAIL'}")

    os.makedirs("out", exist_ok=True)
    pfad = f"out/01_latenz_optimize-{optimize}.json"
    json.dump({"seed": SEED, "n": n, "optimize": optimize,
               "p95_s": p95, "median_s": statistics.median(ttfas),
               "rtf_median": statistics.median(rtfs), "roh": roh},
              open(pfad, "w"), indent=1)
    print(f"Rohwerte: {pfad}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=50)
    ap.add_argument("--no-optimize", action="store_true")
    ap.add_argument("--kaltstart", action="store_true")
    a = ap.parse_args()
    opt = not a.no_optimize
    if a.kaltstart:
        kaltstart(opt)
    else:
        messreihe(a.n, opt)
