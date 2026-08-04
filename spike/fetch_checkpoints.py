"""Checkpoints bei festen Revisionen holen. Phase-0-Plan Schritt 2.

Nach diesem Skript darf zur Laufzeit nichts mehr ins Netz: die Skripte setzen
HF_HUB_OFFLINE=1 und local_files_only, und finden die Dateien im Cache.
"""

import sys

import yaml
from huggingface_hub import snapshot_download

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]


def main(names: list[str]) -> int:
    for name in names or REVS:
        cfg = REVS[name]
        path = snapshot_download(repo_id=cfg["repo"], revision=cfg["revision"])
        print(f"{name}: {cfg['repo']}@{cfg['revision'][:12]} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
