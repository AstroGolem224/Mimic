"""Legt jedem Entwurf die Kette auf, die in seiner Stimmendatei steht.

`faerben.py` kann jede Kette auf jede Datei werfen. Das hier ist der Weg fuer
den Regelfall: die Stimmendatei sagt, welche Kette zu welcher Figur gehoert,
und dieses Skript zieht das ueber alle Kandidaten durch.

  uv run python veredeln.py stimmen_wild.yaml

Ausgabe: out/gefaerbt/<id>_<k>__<kette>.wav
"""

from __future__ import annotations

import pathlib
import sys

import yaml

from faerben import KETTEN, faerben

WURZEL = pathlib.Path(__file__).resolve().parent
EIN = WURZEL / "out" / "entwurf"
AUS = WURZEL / "out" / "gefaerbt"


def main() -> None:
    quelle = WURZEL / sys.argv[1]
    stimmen = yaml.safe_load(quelle.read_text())["stimmen"]
    AUS.mkdir(parents=True, exist_ok=True)

    for stimme in stimmen:
        kette = stimme.get("kette")
        if kette is None:
            print(f"{stimme['id']}  keine Kette eingetragen, uebersprungen")
            continue
        if kette not in KETTEN:
            print(f"{stimme['id']}  unbekannte Kette {kette!r}, uebersprungen")
            continue
        for pfad in sorted(EIN.glob(f"{stimme['id']}_*.wav")):
            ziel = AUS / f"{pfad.stem}__{kette}.wav"
            faerben(pfad, kette, ziel)
            print(ziel.name)


if __name__ == "__main__":
    main()
