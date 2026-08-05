"""Wie viele Aufrufe nach einem Warmlauf bis zum eingeschwungenen TTFA?

Anlass: `/warm` schickt seit Phase 2b einmal Referenzaudio durch, damit der
numba-JIT bezahlt ist (vorher 10 596 ms beim ersten Aufruf). Danach gemessen:
676 ms beim ersten, ~220 ms ab dem zweiten. Die Frage ist, ob dieser Rest echt
und reproduzierbar ist -- denn dAImon setzt laut Plan eine Gesamtfrist von
500 ms. Liegt der erste Aufruf darüber, fällt die erste Äußerung nach jedem
Warmlauf auf sherpa zurück, und der Warmlauf hilft erst der zweiten.

Eine frühere Messung ergab 480/382/562 ms -- die war aber wertlos, weil
gleichzeitig ein zweiter TTS-Dienst die GPU hielt, dAImons Testsuite lief und
die Load bei 3.79 stand. Dieses Skript prüft die Ruhe vorher und bricht sonst ab.

Aufruf: uv run --no-project --python 3.12 python tools/messreihe_warmrampe.py [-z 3] [-n 12]
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mimic.frontend import frontend_socket_path  # noqa: E402

MAX_LOAD = 2.5          # darüber ist die Maschine nicht ruhig genug
MIN_FREI_MIB = 6000
TEXT = "Ein gleichbleibender Satz, damit die Aufrufe vergleichbar bleiben."


class Unix(http.client.HTTPConnection):
    def __init__(self, pfad, timeout=120):
        super().__init__("localhost", timeout=timeout)
        self.pfad = str(pfad)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.pfad)


def ruf(methode: str, pfad: str, koerper: dict | None = None) -> tuple[int, bytes, float]:
    c = Unix(frontend_socket_path())
    roh = json.dumps(koerper).encode() if koerper else None
    kopf = ({"Content-Type": "application/json", "Content-Length": str(len(roh))}
            if roh else {})
    t0 = time.perf_counter()
    c.request(methode, pfad, roh, kopf)
    antwort = c.getresponse()
    daten = antwort.read()
    return antwort.status, daten, (time.perf_counter() - t0) * 1000


def frei_mib() -> int:
    for zeile in open("/proc/meminfo"):
        if zeile.startswith("MemAvailable"):
            return int(zeile.split()[1]) // 1024
    return 0


def ruhig() -> tuple[bool, str]:
    load = os.getloadavg()[0]
    if load > MAX_LOAD:
        return False, f"Load {load:.2f} > {MAX_LOAD}"
    if frei_mib() < MIN_FREI_MIB:
        return False, f"nur {frei_mib()} MiB RAM frei"
    return True, f"Load {load:.2f}, {frei_mib()} MiB frei"


def worker_neu() -> None:
    for befehl in (["stop", "mimic-worker.service"],
                   ["reset-failed", "mimic-worker.socket", "mimic-worker.service"],
                   ["restart", "mimic-worker.socket"]):
        subprocess.run(["systemctl", "--user", *befehl],
                       capture_output=True, check=False)
    time.sleep(1)


def ttfa_aus_journal(anzahl: int) -> list[float]:
    aus = subprocess.run(["journalctl", "--user", "-u", "mimic-worker.service",
                          "--since", "-5min", "--no-pager"],
                         capture_output=True, text=True).stdout
    werte = [float(z.split("ttfa_ms=")[1].split()[0])
             for z in aus.splitlines() if "ttfa_ms=" in z]
    return werte[-anzahl:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-z", type=int, default=3, help="Zyklen aus Kaltstart")
    ap.add_argument("-n", type=int, default=12, help="Aufrufe je Zyklus")
    a = ap.parse_args()

    ok, wie = ruhig()
    print(f"Maschine: {wie}")
    if not ok:
        print("ABBRUCH: zu unruhig. Eine Messung unter Fremdlast ist wertlos.")
        return 2

    rampen: list[list[float]] = []
    for zyklus in range(1, a.z + 1):
        worker_neu()
        status, _, _ = ruf("POST", "/warm", {"mode": "mf"})
        wartete = 0
        for wartete in range(1, 90):
            time.sleep(1)
            if json.loads(ruf("GET", "/status")[1]).get("state") == "warm":
                break
        print(f"\nZyklus {zyklus}: /warm -> HTTP {status}, warm nach ~{wartete} s")
        koerper = {"text": TEXT, "voice": "matthias", "mode": "mf", "require_warm": True}
        for _ in range(a.n):
            code, _, _ = ruf("POST", "/speak", koerper)
            if code != 200:
                print(f"  Aufruf lieferte HTTP {code} -- Zyklus verworfen")
                break
        else:
            werte = ttfa_aus_journal(a.n)
            rampen.append(werte)
            print("  TTFA: " + "  ".join(f"{w:.0f}" for w in werte))

    if not rampen:
        print("Keine gueltigen Zyklen.")
        return 1

    print(f"\n{'Aufruf':>7}  {'median TTFA':>12}   (n={len(rampen)} Zyklen)")
    breite = min(len(r) for r in rampen)
    ueber500 = 0
    for i in range(breite):
        spalte = [r[i] for r in rampen]
        med = statistics.median(spalte)
        marke = "  <- ueber 500 ms" if med > 500 else ""
        if med > 500:
            ueber500 = i + 1
        print(f"{i+1:>7}  {med:>9.0f} ms{marke}")
    print(f"\nEingeschwungen ab Aufruf {ueber500 + 1}. "
          f"dAImons Gesamtfrist ist 500 ms.")
    ziel = Path(__file__).resolve().parent.parent / "out/messreihe_warmrampe.json"
    ziel.parent.mkdir(exist_ok=True)
    ziel.write_text(json.dumps({"zyklen": rampen, "text": TEXT}, indent=1))
    print(f"Rohwerte: {ziel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
