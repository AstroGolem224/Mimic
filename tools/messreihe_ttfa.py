"""TTFA am Socket messen -- die Zahl, die Kriterium C nie gemessen hat.

Kriterium C aus Phase 0 ergab 90.9 ms. Das Spike-Skript rief `generate_stream`
aber OHNE `prompt_audio_path`, also ohne Klonen. Isoliert nachgemessen kostet
das Konditionieren ~97 ms (87.3 -> 184.5 ms). Der Dienst zeigte in einer
Einzelmessung 419 ms, unerklaert.

Dieses Skript misst den ausgelieferten Pfad: durch das Frontend, ueber den
Unix-Socket, mit Stimmprofil, wie ein Konsument es sieht. Messpunkt ist das
erste Byte des ersten A-Rahmens beim Client -- nicht serverintern.

Speicher: der Lauf bricht ab, wenn zu wenig frei ist. Auf dieser Maschine hat
ein unbedachter Lauf schon einmal den Kernel-OOM-Killer geweckt.

Aufruf:  uv run --no-project --python 3.12 python tools/messreihe_ttfa.py [-n 60]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimic.frontend import UnixHTTPConnection, frontend_socket_path  # noqa: E402
from mimic.protocol import read_frame  # noqa: E402

MIN_FREI_MIB = 6000     # darunter nicht starten und nicht weitermessen
SEED = 20260805


def frei_mib() -> int:
    for zeile in open("/proc/meminfo"):
        if zeile.startswith("MemAvailable"):
            return int(zeile.split()[1]) // 1024
    return 0


def perzentil(werte: list[float], p: float) -> float:
    s = sorted(werte)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def eine_messung(text: str, mode: str) -> tuple[float, float, float]:
    """(ttfa_s, gesamt_s, audio_s), gemessen beim Client."""
    body = json.dumps({"text": text, "voice": "matthias", "mode": mode}).encode()
    conn = UnixHTTPConnection(frontend_socket_path(), 180)
    t0 = time.perf_counter()
    conn.request("POST", "/speak", body, {"Content-Type": "application/json",
                                          "Content-Length": str(len(body))})
    response = conn.getresponse()
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status}: {response.read()[:200]!r}")
    ttfa = None
    samples = 0
    try:
        while True:
            kind, payload = read_frame(response)
            if kind == "A":
                if ttfa is None:
                    ttfa = time.perf_counter() - t0
                samples += len(payload) // 2
            elif kind == "E":
                ende = json.loads(payload)
                if ende.get("status") != "ok":
                    raise RuntimeError(f"E-Rahmen meldet {ende}")
                break
    finally:
        response.close()
        conn.close()
    return ttfa, time.perf_counter() - t0, samples / 48_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=60)
    ap.add_argument("--mode", default="mf", choices=("mf", "soar"))
    a = ap.parse_args()

    if frei_mib() < MIN_FREI_MIB:
        print(f"ABBRUCH: nur {frei_mib()} MiB frei, mindestens {MIN_FREI_MIB} noetig.")
        return 2

    # Ein Korpus mit Laengenstreuung -- TTFA soll nicht von einem Satztyp abhaengen.
    korpus = [
        "Fertig.",
        "Das war knapp.",
        "Der Aufzug oeffnet sich.",
        "Die Runde dauerte zwoelf Minuten und siebenundvierzig Sekunden.",
        "Die Charakterstufe springt nur an, wenn genug Grafikspeicher frei ist.",
        "Nach dem letzten Boss oeffnet sich der Aufzug, und du entscheidest, "
        "ob du weitergehst oder umkehrst.",
    ]
    rng = random.Random(SEED)
    plan = [korpus[rng.randrange(len(korpus))] for _ in range(a.n + 3)]

    print(f"Warmlauf (3 Laeufe, verworfen), Modus {a.mode}...")
    for text in plan[:3]:
        eine_messung(text, a.mode)

    print(f"Messreihe n={a.n}, frei {frei_mib()} MiB")
    roh = []
    for i, text in enumerate(plan[3:], 1):
        if frei_mib() < MIN_FREI_MIB:
            print(f"ABBRUCH bei {i}: nur noch {frei_mib()} MiB frei.")
            break
        ttfa, gesamt, audio_s = eine_messung(text, a.mode)
        roh.append({"i": i, "chars": len(text), "ttfa_s": ttfa,
                    "gesamt_s": gesamt, "audio_s": audio_s,
                    "rtf": gesamt / audio_s if audio_s else None})
        if i % 10 == 0:
            print(f"  {i}/{a.n}   frei {frei_mib()} MiB")

    ttfas = [r["ttfa_s"] for r in roh]
    rtfs = [r["rtf"] for r in roh if r["rtf"]]
    p95 = perzentil(ttfas, 0.95)
    print(f"""
TTFA am Socket, mit Klonen, Modus {a.mode}, n={len(ttfas)}
  min  {min(ttfas)*1000:7.1f} ms
  med  {statistics.median(ttfas)*1000:7.1f} ms
  p95  {p95*1000:7.1f} ms      (lineare Interpolation)
  max  {max(ttfas)*1000:7.1f} ms
RTF med {statistics.median(rtfs):.4f}

Kriterium C (p95 < 300 ms): {'PASS' if p95 < 0.300 else 'FAIL'}""")

    ziel = Path(__file__).resolve().parent.parent / f"out/messreihe_ttfa_{a.mode}.json"
    ziel.parent.mkdir(exist_ok=True)
    ziel.write_text(json.dumps({"n": len(ttfas), "mode": a.mode, "seed": SEED,
                                "p95_s": p95, "median_s": statistics.median(ttfas),
                                "rtf_median": statistics.median(rtfs), "roh": roh}, indent=1))
    print(f"Rohwerte: {ziel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
