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
import array
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mimic.frontend import UnixHTTPConnection, frontend_socket_path  # noqa: E402
from mimic.protocol import read_frame  # noqa: E402

STUMM_PEAK = int(32768 * 10 ** (-25.0 / 20))   # gleiche Schwelle wie der Worker
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


STIMME = "matthias"


def erster_lauter_index(pcm: bytes) -> int | None:
    """Index des ersten Samples ueber STUMM_PEAK, oder None."""
    werte = array.array("h")
    werte.frombytes(pcm)
    for i, wert in enumerate(werte):
        if abs(wert) > STUMM_PEAK:
            return i
    return None


def eine_messung(text: str, mode: str) -> tuple[float, float, float, float]:
    """(ttfa_s, hoerbar_s, gesamt_s, audio_s), gemessen beim Client.

    `hoerbar_s` ist die Zahl, die P2-F verlangt: wann der Hoerer Ton hoert.
    Der Konsument spielt ab Ankunft des ersten Rahmens, also liegt Sample k
    bei ttfa + k/48000 -- fuehrende Stille im Rahmen zaehlt mit.
    """
    body = json.dumps({"text": text, "voice": STIMME, "mode": mode}).encode()
    conn = UnixHTTPConnection(frontend_socket_path(), 180)
    t0 = time.perf_counter()
    conn.request("POST", "/speak", body, {"Content-Type": "application/json",
                                          "Content-Length": str(len(body))})
    response = conn.getresponse()
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status}: {response.read()[:200]!r}")
    ttfa = None
    hoerbar = None
    samples = 0
    try:
        while True:
            kind, payload = read_frame(response)
            if kind == "A":
                if ttfa is None:
                    ttfa = time.perf_counter() - t0
                if hoerbar is None:
                    index = erster_lauter_index(payload)
                    if index is not None:
                        hoerbar = ttfa + (samples + index) / 48_000
                samples += len(payload) // 2
            elif kind == "E":
                ende = json.loads(payload)
                if ende.get("status") != "ok":
                    raise RuntimeError(f"E-Rahmen meldet {ende}")
                break
    finally:
        response.close()
        conn.close()
    return ttfa, hoerbar, time.perf_counter() - t0, samples / 48_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=60)
    ap.add_argument("--mode", default="mf", choices=("mf", "soar"))
    ap.add_argument("--voice", default="matthias")
    # Rev 9: gemessen wird der Korpus, den Mimic unter dAImons Auswahlregel
    # wirklich bekommt. Kurze Saetze gehen an die Vorgabestufe; sie in die Zahl
    # zu mischen hat die Messung vom 06.08. mit verzerrt.
    ap.add_argument("--ab-zeichen", type=int, default=0,
                    help="nur Texte ab dieser Laenge (Rev 9: 80)")
    a = ap.parse_args()
    global STIMME
    STIMME = a.voice

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
        "Die Charakterstufe springt nur an, wenn genug Grafikspeicher frei ist "
        "und kein Spiel im Vollbild laeuft.",
        "Ich habe den Fehler gefunden: der Dienst lief aus einer eingefrorenen "
        "Kopie und nicht aus dem Arbeitsbaum.",
        "Das Modell stellt der Aeusserung manchmal eine halbe Sekunde Stille "
        "voran, und niemand hat das je gemessen.",
    ]
    if a.ab_zeichen:
        korpus = [t for t in korpus if len(t) >= a.ab_zeichen]
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
        ttfa, hoerbar, gesamt, audio_s = eine_messung(text, a.mode)
        roh.append({"i": i, "chars": len(text), "ttfa_s": ttfa,
                    "hoerbar_s": hoerbar, "gesamt_s": gesamt, "audio_s": audio_s,
                    "rtf": gesamt / audio_s if audio_s else None})
        if i % 10 == 0:
            print(f"  {i}/{a.n}   frei {frei_mib()} MiB")

    ttfas = [r["ttfa_s"] for r in roh]
    hoerbare = [r["hoerbar_s"] for r in roh if r["hoerbar_s"]]
    p95_h = perzentil(hoerbare, 0.95)
    # Rev 9 (b): wie oft kommt Mimic durch, bevor dAImon nach 500 ms zurueckfaellt.
    anteil = sum(1 for t in ttfas if t < 0.500) / len(ttfas)
    rtfs = [r["rtf"] for r in roh if r["rtf"]]
    p95 = perzentil(ttfas, 0.95)
    print(f"""
TTFA am Socket, mit Klonen, Modus {a.mode}, n={len(ttfas)}
  min  {min(ttfas)*1000:7.1f} ms
  med  {statistics.median(ttfas)*1000:7.1f} ms
  p95  {p95*1000:7.1f} ms      (lineare Interpolation)
  max  {max(ttfas)*1000:7.1f} ms

Bis zum ersten hoerbaren Sample -- das ist P2-F
  med  {statistics.median(hoerbare)*1000:7.1f} ms
  p95  {p95_h*1000:7.1f} ms
  max  {max(hoerbare)*1000:7.1f} ms
RTF med {statistics.median(rtfs):.4f}

P2-F Rev 9, Korpus ab {a.ab_zeichen or 0} Zeichen
  (a) Median hoerbar < 300 ms          {statistics.median(hoerbare)*1000:6.0f} ms   {'PASS' if statistics.median(hoerbare) < 0.300 else 'FAIL'}
  (b) Anteil Ankunft < 500 ms >= 90 %  {anteil*100:6.1f} %    {'PASS' if anteil >= 0.90 else 'FAIL'}""")

    ziel = Path(__file__).resolve().parent.parent / f"out/messreihe_ttfa_{a.mode}_{a.voice}.json"
    ziel.parent.mkdir(exist_ok=True)
    ziel.write_text(json.dumps({"n": len(ttfas), "mode": a.mode, "seed": SEED,
                                "p95_s": p95, "p95_hoerbar_s": p95_h, "anteil_unter_500ms": anteil, "median_s": statistics.median(ttfas),
                                "rtf_median": statistics.median(rtfs), "roh": roh}, indent=1))
    print(f"Rohwerte: {ziel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
