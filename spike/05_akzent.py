"""Kriterium D — Akzent-Leakage, relativ gegen die eigene Baseline.

Gemessen wird NICHT, ob Mimic deutschen Akzent im Englischen hat. Matthias hat
einen; ein Klon, der ihn nicht haette, waere kein Klon. Gemessen wird, ob Mimic
an den vorab markierten Stellen HOERBAR DEUTSCHER klingt als Matthias selbst
an derselben Stelle.

Ablauf je Satz: zwei Proben, A und B, in zufaelliger Reihenfolge -- eine echt,
eine Mimic. Frage: klingt eine der beiden an den markierten Stellen deutlich
deutscher? Wenn ja, welche? Nur "B klingt deutscher UND B war Mimic" zaehlt
als Treffer.

Bestanden bei HOECHSTENS 2 Treffern von 10.

Aufruf:
  uv run python 05_akzent.py erzeugen
  uv run python 05_akzent.py hoeren
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import yaml  # noqa: E402

CORPUS = yaml.safe_load(open("corpus.yaml"))
REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
STIMME = os.path.expanduser("~/.local/share/mimic/voices/matthias")
BASELINE = "aufnahmen/baseline"
MIMIC = "out/akzent_mimic"
ERGEBNIS = "out/akzent_ergebnis.json"

ZIEL_RMS_DBFS = -23.0
BESTANDEN_MAX_TREFFER = 2


def angleichen_datei(quelle: str, ziel: str) -> None:
    x, sr = sf.read(quelle, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = x.astype(np.float64)
    rms = float(np.sqrt(np.mean(x**2))) or 1e-9
    x = x * (10 ** (ZIEL_RMS_DBFS / 20) / rms)
    peak = float(np.max(np.abs(x)))
    if peak > 0.98:
        x = x * (0.98 / peak)
    os.makedirs(os.path.dirname(ziel), exist_ok=True)
    sf.write(ziel, x.astype(np.float32), sr)


def erzeugen() -> int:
    ref_wav, ref_txt = f"{STIMME}/ref.wav", f"{STIMME}/ref.txt"
    if not os.path.exists(ref_wav):
        print(f"FEHLT: {ref_wav}\n  -> uv run python 02_aufnehmen.py referenz")
        return 2
    eintraege = CORPUS["akzent_check"]
    fehlend = [e["id"] for e in eintraege
               if not os.path.exists(f"{BASELINE}/{e['id']}.wav")]
    if fehlend:
        print(f"Baseline fehlt: {', '.join(fehlend)}"
              f"\n  -> uv run python 02_aufnehmen.py akzent")
        return 2

    import laden
    rt = laden.runtime("soar")
    prompt_text = open(ref_txt).read().strip()

    os.makedirs(MIMIC, exist_ok=True)
    for e in eintraege:
        # Deutsche Referenz, englischer Text, language="en" -- genau die
        # Konstellation, in der Akzent-Leakage entsteht.
        out = rt.generate(text=e["text"], language="en",
                          prompt_audio_path=ref_wav, prompt_text=prompt_text)
        sf.write(f"{MIMIC}/{e['id']}.wav",
                 out["audio"].squeeze().float().cpu().numpy(), out["sample_rate"])
        print(f"  {e['id']}")
    print(f"\n{len(eintraege)} Proben in {MIMIC}/")
    print("Weiter mit:  uv run python 05_akzent.py hoeren")
    return 0


def hoeren() -> int:
    if not os.path.isdir(MIMIC) or not os.listdir(MIMIC):
        print("Mimic-Proben fehlen.\n  -> uv run python 05_akzent.py erzeugen")
        return 2
    eintraege = CORPUS["akzent_check"]
    rng = random.SystemRandom()
    print(f"""
{'='*72}
KRITERIUM D -- Akzent-Leakage, relativ

Je Satz hoerst du zwei Proben, A und B. Eine bist du, eine ist Mimic --
in zufaelliger Reihenfolge, du erfaehrst es erst am Ende.

Die Frage ist NICHT "welche ist Mimic". Die Frage ist:
klingt eine der beiden an den markierten Stellen DEUTLICH DEUTSCHER?

Dein eigener Akzent ist kein Fehler. Nur ein Klon, der staerker deutschelt
als du selbst, ist einer.

  [a] A klingt deutscher   [b] B klingt deutscher
  [g] kein Unterschied     [w] beide nochmal
Bestanden bei hoechstens {BESTANDEN_MAX_TREFFER} Treffern von {len(eintraege)}.
{'='*72}""")

    protokoll, treffer = [], 0
    for i, e in enumerate(eintraege, 1):
        paar = [("echt", f"{BASELINE}/{e['id']}.wav"),
                ("mimic", f"{MIMIC}/{e['id']}.wav")]
        rng.shuffle(paar)
        tmp = {}
        for label, quelle in zip("AB", [p[1] for p in paar]):
            tmp[label] = f"out/akzent_tmp/{e['id']}_{label}.wav"
            angleichen_datei(quelle, tmp[label])

        print(f"\n{'-'*72}\n[{i}/{len(eintraege)}]  {e['id']}")
        print(textwrap.fill(e["text"], 68, initial_indent="  » ", subsequent_indent="    "))
        print(f"  Risikostellen: {', '.join(e['risiko'])}")
        while True:
            for label in "AB":
                print(f"    {label} ...", end="", flush=True)
                subprocess.run(["pw-cat", "-p", tmp[label]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(" ok")
            a = input("  [a/b/g/w]: ").strip().lower()
            if a == "w":
                continue
            if a in ("a", "b", "g"):
                break
        gewaehlt = None if a == "g" else paar["ab".index(a)][0]
        ist_treffer = gewaehlt == "mimic"
        treffer += ist_treffer
        protokoll.append({"id": e["id"], "reihenfolge": [p[0] for p in paar],
                          "antwort": a, "deutscher": gewaehlt, "treffer": ist_treffer})
        print(f"    -> {'TREFFER (Mimic deutscher)' if ist_treffer else 'kein Treffer'}")

    bestanden = treffer <= BESTANDEN_MAX_TREFFER
    print(f"\n{'='*72}\n  {treffer} Treffer von {len(eintraege)}")
    print(f"  Kriterium D (<= {BESTANDEN_MAX_TREFFER}): {'PASS' if bestanden else 'FAIL'}")
    json.dump({"treffer": treffer, "gesamt": len(eintraege),
               "bestanden": bestanden, "protokoll": protokoll},
              open(ERGEBNIS, "w"), indent=1)
    print(f"  {ERGEBNIS}")
    return 0 if bestanden else 1


if __name__ == "__main__":
    modi = {"erzeugen": erzeugen, "hoeren": hoeren}
    if len(sys.argv) != 2 or sys.argv[1] not in modi:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(modi[sys.argv[1]]())
