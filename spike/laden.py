"""Ein Ladepfad fuer alle Spike-Skripte.

Warum das hier steht statt fuenfmal `DotsTtsRuntime.from_pretrained(...)`:

`from_pretrained` baut das Modell mit den Torch-Defaults, also auf der CPU in
fp32. Bei ~2B Parametern sind das rund 8 GB Systemspeicher, die gleich darauf
wieder wegfallen, wenn das Modell nach cuda/bfloat16 wandert. Auf einer
30-GiB-Maschine mit laufendem Desktop hat genau das am 2026-08-04 einen
Kernel-OOM ausgeloest (`anon-rss 10315868 kB`).

Ein `torch.device("cuda")`-Kontext um die Konstruktion laesst die Huelle direkt
auf der GPU entstehen. Gemessen (09_direktladen.py, mf, MemoryMax hart):

                    RAM-Spitze   RAM Ruhe   Ladezeit
  Torch-Defaults      12479 MiB   4357 MiB    14.5 s   -- OOM unter 12 GB Limit
  device=cuda          5514 MiB   2269 MiB     4.3 s   -- laeuft unter 4 GB

Nur der dtype-Default zu setzen reicht nicht (OOM bei 6 GB) -- es ist die
Device-Platzierung, nicht die Praezision.

Das State-Dict geht weiterhin ueber die CPU: `model.py:350` haelt
`load_file(path, device="cpu")` fest. Das ist der verbleibende Rest der Spitze
und waere nur mit einem Upstream-Patch wegzubekommen.
"""

from __future__ import annotations

import os

import torch
import yaml

REVS = yaml.safe_load(
    open(os.path.join(os.path.dirname(__file__), "revisions.yaml"))
)["checkpoints"]


def runtime(name: str = "mf", *, optimize: bool = False, direkt: bool = True):
    """Laedt einen Checkpoint. `direkt=False` nur fuer den Vergleichsfall."""
    from dots_tts.runtime import DotsTtsRuntime

    c = REVS[name]
    bauen = lambda: DotsTtsRuntime.from_pretrained(
        c["repo"], revision=c["revision"], precision="bfloat16", optimize=optimize)

    if not direkt:
        return bauen()
    with torch.device("cuda"):
        return bauen()
