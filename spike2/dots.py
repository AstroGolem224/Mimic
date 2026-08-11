"""Kontrollgruppe: dieselben Texte durch den laufenden Mimic-Dienst.

Ohne die ist der Blindtest wertlos -- man hoerte nur, welche der zwei neuen
Engines besser klingt, nicht ob eine davon besser ist als das, was schon da
ist. Laeuft in der Hauptumgebung, nicht in einer der spike2-Umgebungen:

  uv run python spike2/dots.py

Nur `matthias`: fuer den MOSS-Entwurf gibt es kein Mimic-Stimmprofil, und eins
anzulegen waere eine andere Frage als die hier gestellte.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import yaml

WURZEL = pathlib.Path(__file__).resolve().parent
TEXTE = yaml.safe_load((WURZEL / "texte.yaml").read_text())["texte"]
AUS = WURZEL / "out" / "klon" / "dots"
STIMME = "matthias"


def main() -> None:
    AUS.mkdir(parents=True, exist_ok=True)
    for eintrag in TEXTE:
        ziel = AUS / f"{STIMME}_{eintrag['id']}.wav"
        # `soar` statt `mf`: gespeicherte Datei, keine Echtzeit -- derselbe
        # Betriebspunkt, den die GUI beim Exportieren waehlt.
        ergebnis = subprocess.run(
            ["mimic", "say", eintrag["text"], "--voice", STIMME, "--mode", "soar",
             "-o", str(ziel)],
            capture_output=True, text=True,
        )
        if ergebnis.returncode != 0:
            print(f"   {eintrag['id']:18s} FEHLER: {ergebnis.stderr.strip()[:120]}",
                  file=sys.stderr)
            continue
        print(f"   {eintrag['id']:18s} -> {ziel.name}")


if __name__ == "__main__":
    main()
