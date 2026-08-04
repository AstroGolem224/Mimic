"""RSS-Peak beim Laden. Die Zahl, die in Phase 0 gefehlt hat.

Anlass: am 2026-08-04 hat der Kernel einen Spike-Lauf abgeschossen --
`anon-rss 10315868 kB`, also gut 10 GB, bei 30 GiB Gesamt-RAM und einem
Desktop, der davon schon 16 GiB haelt. Der Befund aus 03_modewechsel
("beide Checkpoints resident, 11.2 GiB von 32 -- passt") war eine reine
VRAM-Messung und deckt diesen Fall nicht.

Unterschieden werden zwei Zahlen, weil sie unterschiedliche Konsequenzen haben:

  VmHWM   Hochwassermarke. Bestimmt, ob der Ladevorgang ueberhaupt durchkommt,
          und damit MemoryMax.
  VmRSS   was nach dem Laden dauerhaft haengen bleibt. Bestimmt, was der
          Dienst im Leerlauf kostet, und damit MemoryHigh.

Aufruf:  uv run python 08_ram.py mf
         uv run python 08_ram.py soar
Bewusst EIN Checkpoint pro Prozess -- beide gleichzeitig ist genau der Lauf,
der die Maschine umgebracht hat.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import yaml  # noqa: E402

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]


def proc_kib(feld: str) -> int:
    for zeile in open("/proc/self/status"):
        if zeile.startswith(feld):
            return int(zeile.split()[1])
    return 0


def frei_mib() -> int:
    for zeile in open("/proc/meminfo"):
        if zeile.startswith("MemAvailable"):
            return int(zeile.split()[1]) // 1024
    return 0


def mib(kib: int) -> float:
    return kib / 1024


def main(name: str) -> int:
    print(f"vor allem      RSS {mib(proc_kib('VmRSS')):7.0f} MiB   "
          f"frei im System {frei_mib()} MiB")

    from dots_tts.runtime import DotsTtsRuntime
    print(f"nach import    RSS {mib(proc_kib('VmRSS')):7.0f} MiB")

    c = REVS[name]
    t0 = time.perf_counter()
    rt = DotsTtsRuntime.from_pretrained(
        c["repo"], revision=c["revision"], precision="bfloat16", optimize=False)
    dauer = time.perf_counter() - t0

    hwm, rss = proc_kib("VmHWM"), proc_kib("VmRSS")
    print(f"nach laden     RSS {mib(rss):7.0f} MiB   HWM {mib(hwm):7.0f} MiB   "
          f"({dauer:.1f} s)")

    rt.generate(text="Ein kurzer Satz, damit auch der Inferenzpfad Speicher sieht.",
                language="de")
    hwm2, rss2 = proc_kib("VmHWM"), proc_kib("VmRSS")
    print(f"nach inferenz  RSS {mib(rss2):7.0f} MiB   HWM {mib(hwm2):7.0f} MiB")

    print(f"""
-- Befund fuer {name} {'-'*(56-len(name))}
MemoryMax  muss ueber {mib(hwm2):.0f} MiB liegen, sonst stirbt der Ladevorgang.
MemoryHigh sinnvoll um {mib(rss2):.0f} MiB, das ist der Ruhezustand.
frei im System jetzt: {frei_mib()} MiB
""")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in REVS:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
