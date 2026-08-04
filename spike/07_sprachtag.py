"""Schadet das [EN]-Tag reinem Deutsch?

Aus 06 kam heraus: bei deutschen Saetzen mit englischen Fachbegriffen liefert
`language="en"` das beste Ergebnis -- die Begriffe klingen englisch, das
Deutsche bleibt tragbar. Segmentierung ist tot (kurze Fragmente ohne
Satzkontext halluziniert das Modell voll).

Damit haengt der Phase-1-Entwurf an genau einer Frage:

  Schadet [EN] einem Satz OHNE englische Begriffe?

  nein  -> trivial. Deutsch mit Englisch-Anteil faehrt immer `en`, fertig.
  ja    -> es braucht Erkennung pro Aeusserung, und die muss in den
           Phase-1-Plan als eigener Baustein.

Rendert jeden Satz zweimal, einmal je Tag, und legt sie als Paar ab.

Aufruf:  uv run python 07_sprachtag.py
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import soundfile as sf  # noqa: E402
import yaml  # noqa: E402

CORPUS = yaml.safe_load(open("corpus.yaml"))
REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
STIMME = os.path.expanduser("~/.local/share/mimic/voices/matthias")
OUT = "out/sprachtag"

# (id, gruppe) -- Gruppe sagt, was der Satz beweisen soll.
AUSWAHL = [
    ("de_lang_01", "rein_deutsch"),
    ("de_zahlen_01", "rein_deutsch"),
    ("de_komposita_02", "rein_deutsch"),
    ("de_code_switching_01", "gemischt"),
    ("de_code_switching_02", "gemischt"),
    ("en_lang_01", "rein_englisch"),
]


def main() -> int:
    ref_wav, ref_txt = f"{STIMME}/ref.wav", f"{STIMME}/ref.txt"
    if not os.path.exists(ref_wav):
        print(f"FEHLT: {ref_wav}")
        return 2
    prompt_text = open(ref_txt).read().strip()
    texte = {e["id"]: e["text"] for e in CORPUS["de"] + CORPUS["en"]}

    import laden
    rt = laden.runtime("soar")
    os.makedirs(OUT, exist_ok=True)

    for sid, gruppe in AUSWAHL:
        text = texte[sid]
        print(f"\n{gruppe:14s} {sid}\n  » {text[:66]}")
        for tag in ("de", "en"):
            out = rt.generate(text=text, language=tag, prompt_audio_path=ref_wav,
                              prompt_text=prompt_text)
            audio = out["audio"].squeeze().float().cpu().numpy()
            pfad = f"{OUT}/{gruppe}__{sid}__{tag}.wav"
            sf.write(pfad, audio, out["sample_rate"])
            print(f"    [{tag.upper()}] {len(audio)/out['sample_rate']:5.1f} s  {pfad}")

    print(f"""
Je Satz zwei Dateien, __de und __en. Vergleichen, gruppenweise:

  rein_deutsch    Klingt __en schlechter als __de? Wenn nein, ist die Sache
                  erledigt: immer `en` fahren.
  gemischt        Bestaetigt sich der Befund aus 06?
  rein_englisch   Kontrolle -- hier muss __en gewinnen, sonst stimmt am
                  Messaufbau etwas nicht.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
