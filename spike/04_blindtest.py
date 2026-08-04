"""Kriterium B — verblindete Echt/Synthetisch-Unterscheidung.

Konstruktion laut Plan, vor dem Test eingefroren:
  12 Proben, 6 echt und 6 von Mimic, gleiche sechs Texte in beiden Haelften,
  Texte nicht aus dem Referenzmaterial, Reihenfolge zufaellig, Labels vom
  Skript vergeben und bis zum Ende verdeckt.

Bestanden bei HOECHSTENS 8 von 12 richtig. Das ist kein Tippfehler: gemessen
wird, ob der Klon *nicht* zuverlaessig von echt zu unterscheiden ist. 12/12
richtig heisst, du hoerst den Unterschied jedes Mal -- das ist ein Durchfall.

Pegelangleich ist Pflicht, nicht Kosmetik: ein Lautstaerkeunterschied zwischen
den Haelften waere ein Hinweis, der nichts mit Stimmqualitaet zu tun hat, und
wuerde den Test still zu einem Lautstaerketest machen.

Aufruf:
  uv run python 04_blindtest.py erzeugen   # Mimic-Haelfte rendern
  uv run python 04_blindtest.py hoeren     # der eigentliche Test
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import yaml  # noqa: E402

CORPUS = yaml.safe_load(open("corpus.yaml"))
REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
STIMME = os.path.expanduser("~/.local/share/mimic/voices/matthias")
ECHT = "aufnahmen/echt"
MIMIC = "out/blindtest_mimic"
PROBEN = "out/blindtest_proben"
SCHLUESSEL = "out/blindtest_schluessel.json"
ERGEBNIS = "out/blindtest_ergebnis.json"

ZIEL_RMS_DBFS = -23.0
BESTANDEN_MAX_RICHTIG = 8


def angleichen(x: np.ndarray) -> np.ndarray:
    """RMS auf Zielpegel, danach Peak-Begrenzung. Nimmt der Lautstaerke die
    Rolle als Hinweisgeber."""
    x = x.astype(np.float64)
    rms = float(np.sqrt(np.mean(x**2))) or 1e-9
    x = x * (10 ** (ZIEL_RMS_DBFS / 20) / rms)
    peak = float(np.max(np.abs(x)))
    if peak > 0.98:
        x = x * (0.98 / peak)
    return x.astype(np.float32)


def erzeugen() -> int:
    ref_wav, ref_txt = f"{STIMME}/ref.wav", f"{STIMME}/ref.txt"
    for p in (ref_wav, ref_txt):
        if not os.path.exists(p):
            print(f"FEHLT: {p}\n  -> uv run python 02_aufnehmen.py referenz")
            return 2
    texte = {e["id"]: e["text"] for e in CORPUS["de"]}
    ids = CORPUS["blindtest"]
    fehlend = [i for i in ids if not os.path.exists(f"{ECHT}/{i}.wav")]
    if fehlend:
        print(f"Echte Aufnahmen fehlen: {', '.join(fehlend)}"
              f"\n  -> uv run python 02_aufnehmen.py blindtest")
        return 2

    import laden
    rt = laden.runtime("soar")
    prompt_text = open(ref_txt).read().strip()

    os.makedirs(MIMIC, exist_ok=True)
    for sid in ids:
        out = rt.generate(text=texte[sid], language=laden.SPRACH_TAG,
                          prompt_audio_path=ref_wav, prompt_text=prompt_text)
        sf.write(f"{MIMIC}/{sid}.wav",
                 out["audio"].squeeze().float().cpu().numpy(), out["sample_rate"])
        print(f"  {sid}")
    print(f"\n{len(ids)} Mimic-Proben in {MIMIC}/")
    print("Weiter mit:  uv run python 04_blindtest.py hoeren")
    return 0


def bauen() -> list[dict]:
    """12 pegelangeglichene Proben in zufaelliger Reihenfolge. Der Schluessel
    wird geschrieben, aber erst am Ende gezeigt."""
    ids = CORPUS["blindtest"]
    roh = [(sid, "echt", f"{ECHT}/{sid}.wav") for sid in ids] + \
          [(sid, "mimic", f"{MIMIC}/{sid}.wav") for sid in ids]
    rng = random.SystemRandom()      # kein fester Seed: der Test soll nicht
    rng.shuffle(roh)                 # zwischen Laeufen vorhersagbar sein
    os.makedirs(PROBEN, exist_ok=True)
    schluessel = []
    for n, (sid, art, quelle) in enumerate(roh, 1):
        x, sr = sf.read(quelle, dtype="float32", always_2d=False)
        if x.ndim > 1:
            x = x.mean(axis=1)
        ziel = f"{PROBEN}/probe_{n:02d}.wav"
        sf.write(ziel, angleichen(x), sr)
        schluessel.append({"n": n, "id": sid, "art": art, "datei": ziel})
    json.dump(schluessel, open(SCHLUESSEL, "w"), indent=1)
    return schluessel


def hoeren() -> int:
    if not os.path.isdir(MIMIC) or not os.listdir(MIMIC):
        print("Mimic-Haelfte fehlt.\n  -> uv run python 04_blindtest.py erzeugen")
        return 2
    schluessel = bauen()
    print(f"""
{'='*72}
KRITERIUM B -- verblindete Echt/Synthetisch-Unterscheidung

{len(schluessel)} Proben, gemischt. Je Probe: ist das deine echte Stimme oder Mimic?
Raten ist erlaubt und erwartet -- genau darum geht es.

Bestanden bei HOECHSTENS {BESTANDEN_MAX_RICHTIG} von {len(schluessel)} richtig.
Wenn du jedes Mal richtig liegst, ist der Klon durchgefallen, nicht du.

  [e] echt    [m] Mimic    [w] nochmal hoeren
{'='*72}""")

    antworten = []
    for e in schluessel:
        while True:
            subprocess.run(["pw-cat", "-p", e["datei"]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            a = input(f"  Probe {e['n']:2d}/{len(schluessel)}  [e/m/w]: ").strip().lower()
            if a == "w":
                continue
            if a in ("e", "m"):
                antworten.append("echt" if a == "e" else "mimic")
                break

    richtig = sum(a == e["art"] for a, e in zip(antworten, schluessel))
    bestanden = richtig <= BESTANDEN_MAX_RICHTIG

    print(f"\n{'='*72}\nAufloesung\n")
    for a, e in zip(antworten, schluessel):
        mark = "richtig" if a == e["art"] else "  daneben"
        print(f"  {e['n']:2d}  war {e['art']:5s}  du sagtest {a:5s}  {mark}   {e['id']}")
    print(f"\n  {richtig} von {len(schluessel)} richtig")
    print(f"  Kriterium B (<= {BESTANDEN_MAX_RICHTIG}): {'PASS' if bestanden else 'FAIL'}")
    if not bestanden:
        print("\n  Der Klon ist unterscheidbar. Bevor das Modell verworfen wird:\n"
              "  woran hast du es gehoert -- an der Klangfarbe oder am Sprechrhythmus?\n"
              "  Klangfarbe -> anderes Modell. Rhythmus -> Feintuning, anderer Plan.")

    json.dump({"richtig": richtig, "gesamt": len(schluessel), "bestanden": bestanden,
               "antworten": antworten, "schluessel": schluessel},
              open(ERGEBNIS, "w"), indent=1)
    print(f"\n  {ERGEBNIS}")
    return 0 if bestanden else 1


if __name__ == "__main__":
    modi = {"erzeugen": erzeugen, "hoeren": hoeren}
    if len(sys.argv) != 2 or sys.argv[1] not in modi:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(modi[sys.argv[1]]())
