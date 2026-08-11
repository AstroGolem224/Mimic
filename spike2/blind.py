"""Mischt die Klone aus out/klon/<engine>/ zu einem Blindtest.

Der Grund steht in spike/ERGEBNIS.md: zwei Laeufe derselben Bedingung ergaben
1/6 und 6/6, weil beim zweiten bekannt war, was da spielt. Wer weiss, welche
Engine er hoert, hoert nicht mehr die Engine.

  uv run python spike2/blind.py            # mischen, Schluessel wegschreiben
  uv run python spike2/blind.py --aufdecken   # nach dem Hoeren
  uv run python spike2/blind.py --selbsttest  # ohne Dateien, prueft die Zuordnung

Der Schluessel landet ABSICHTLICH nicht in out/blind/, sondern eine Ebene
darueber -- sonst liest man ihn beim Suchen der naechsten Datei versehentlich.
"""

from __future__ import annotations

import csv
import pathlib
import random
import shutil
import subprocess
import sys

WURZEL = pathlib.Path(__file__).resolve().parent
QUELLE = WURZEL / "out" / "klon"
BLIND = WURZEL / "out" / "blind"
SCHLUESSEL = WURZEL / "out" / "blind_schluessel.csv"
# Feste Saat: derselbe Bestand ergibt dieselbe Nummerierung. Wer zwischendurch
# neu generiert, kann seine Notizen also weiterverwenden.
SAAT = 20260811
# Alles auf dieselbe Rate. dots.tts liefert 48 kHz, Chatterbox und Qwen 24 --
# das ist als Bandbreite hoerbar, noch bevor die Stimme einsetzt, und waere
# damit ein Etikett an jeder dritten Datei. Heruntergerechnet wird auf die
# NIEDRIGSTE vorkommende Rate: aufwaerts rechnen holt die fehlende Bandbreite
# nicht zurueck, laesst dots aber trotzdem anders klingen.
BLIND_RATE = 24_000


def sammeln() -> list[tuple[str, pathlib.Path]]:
    """(engine, datei) fuer alles, was unter out/klon/ liegt."""
    return sorted(
        ((pfad.parent.name, pfad) for pfad in QUELLE.glob("*/*.wav")),
        key=lambda paar: (paar[0], paar[1].name),
    )


def mischen(bestand: list, saat: int = SAAT) -> list[tuple[int, str, str]]:
    """(nummer, engine, dateiname) in zufaelliger, aber reproduzierbarer Folge."""
    gemischt = list(bestand)
    random.Random(saat).shuffle(gemischt)
    return [(i, engine, pfad.name if hasattr(pfad, "name") else pfad)
            for i, (engine, pfad) in enumerate(gemischt, start=1)]


def selbsttest() -> None:
    bestand = [(f"engine{i % 3}", f"datei{i}.wav") for i in range(30)]
    zuordnung = mischen(bestand)
    assert len(zuordnung) == len(bestand), "es geht etwas verloren"
    assert {(e, d) for _, e, d in zuordnung} == set(bestand), "Zuordnung stimmt nicht"
    assert [n for n, _, _ in zuordnung] == list(range(1, 31)), "Nummern nicht lueckenlos"
    assert mischen(bestand) == zuordnung, "nicht reproduzierbar"
    assert mischen(bestand, saat=1) != zuordnung, "die Saat tut nichts"
    print("Selbsttest ok")


def aufdecken() -> None:
    if not SCHLUESSEL.exists():
        raise SystemExit("kein Schluessel da -- erst mischen")
    with SCHLUESSEL.open() as datei:
        for zeile in csv.DictReader(datei):
            print(f"{zeile['nummer']:>3s}  {zeile['engine']:12s} {zeile['datei']}")


def main() -> None:
    if "--selbsttest" in sys.argv:
        return selbsttest()
    if "--aufdecken" in sys.argv:
        return aufdecken()

    bestand = sammeln()
    if not bestand:
        raise SystemExit(f"nichts in {QUELLE} -- erst die 00_klonen.py laufen lassen")
    if BLIND.exists():
        shutil.rmtree(BLIND)
    BLIND.mkdir(parents=True)

    zuordnung = mischen(bestand)
    quellen = {(engine, pfad.name): pfad for engine, pfad in bestand}
    for nummer, engine, name in zuordnung:
        ziel = BLIND / f"{nummer:02d}.wav"
        ergebnis = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(quellen[(engine, name)]),
             "-ar", str(BLIND_RATE), "-ac", "1", str(ziel)],
            capture_output=True, text=True,
        )
        if ergebnis.returncode != 0:
            raise SystemExit(f"ffmpeg an {name}: {ergebnis.stderr.strip()[:200]}")

    with SCHLUESSEL.open("w", newline="") as datei:
        schreiber = csv.writer(datei)
        schreiber.writerow(["nummer", "engine", "datei"])
        schreiber.writerows(zuordnung)

    engines = sorted({engine for _, engine, _ in zuordnung})
    print(f"{len(zuordnung)} Dateien aus {len(engines)} Engines ({', '.join(engines)}) "
          f"nach {BLIND}")
    print(f"Schluessel: {SCHLUESSEL} -- erst nach dem Hoeren oeffnen.")


if __name__ == "__main__":
    main()
