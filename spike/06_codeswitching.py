"""Code-Switching — englische Fachbegriffe im deutschen Satz.

Nicht im urspruenglichen Plan als eigener Punkt vorgesehen, aber aus der ersten
Hoerprobe als Problem aufgefallen: bei `language="de"` bekommen englische
Begriffe deutsche Phonetik. Fuer dAImon ist das kein Randfall -- Matthias redet
so den ganzen Tag.

`language` ist in dots.tts nur ein Praefix-Tag am Text (`utils/text.py:77`),
kein Modellschalter. Also sind mehrere Varianten billig testbar:

  de            [DE] vorangestellt -- der aktuelle, schlechte Fall
  none          gar kein Tag, das Modell raet aus dem Kontext
  en            [EN] vorangestellt, obwohl der Satz ueberwiegend deutsch ist
  lautschrift   englische Begriffe deutsch geschrieben -- funktioniert nur da,
                wo der Text vorher bekannt ist (MMC-Batch), nicht bei dAImon
  segmente      Satz an Sprachgrenzen zerlegt, je Segment eigenes Tag,
                Ergebnis aneinandergehaengt

Aufruf:  uv run python 06_codeswitching.py
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import yaml  # noqa: E402

REVS = yaml.safe_load(open("revisions.yaml"))["checkpoints"]
STIMME = os.path.expanduser("~/.local/share/mimic/voices/matthias")
OUT = "out/codeswitching"

SATZ = "Ich habe den Pull Request gemerged, aber das Deployment hängt noch im Staging."

# Englische Begriffe deutsch geschrieben. Kein Anspruch auf Schoenheit -- der
# Punkt ist, ob die Aussprache dadurch richtig wird.
LAUTSCHRIFT = ("Ich habe den Pull Rikwest gemörschd, aber das Diploiment "
               "hängt noch im Steidsching.")

# Fuer die Segment-Variante: (sprache, teiltext) in Lesereihenfolge.
SEGMENTE = [
    ("de", "Ich habe den"),
    ("en", "Pull Request"),
    ("de", "gemerged, aber das"),
    ("en", "Deployment"),
    ("de", "hängt noch im"),
    ("en", "Staging."),
]

PAUSE_MS = 60


def main() -> int:
    ref_wav, ref_txt = f"{STIMME}/ref.wav", f"{STIMME}/ref.txt"
    if not os.path.exists(ref_wav):
        print(f"FEHLT: {ref_wav}")
        return 2
    prompt_text = open(ref_txt).read().strip()

    import laden
    rt = laden.runtime("soar")
    os.makedirs(OUT, exist_ok=True)

    def rendern(text: str, lang: str | None) -> np.ndarray:
        out = rt.generate(text=text, language=lang, prompt_audio_path=ref_wav,
                          prompt_text=prompt_text)
        return out["audio"].squeeze().float().cpu().numpy()

    varianten: list[tuple[str, np.ndarray]] = [
        ("a_de", rendern(SATZ, "de")),
        ("b_none", rendern(SATZ, None)),
        ("c_en", rendern(SATZ, "en")),
        ("d_lautschrift", rendern(LAUTSCHRIFT, "de")),
    ]

    # Segmentweise: jedes Stueck mit eigenem Tag, dann aneinander. Die Naht ist
    # der Preis -- Prosodie laeuft ueber Segmentgrenzen nicht durch.
    stille = np.zeros(int(rt.sample_rate * PAUSE_MS / 1000), dtype=np.float32)
    teile = []
    for lang, text in SEGMENTE:
        teile.append(rendern(text, lang))
        teile.append(stille)
    varianten.append(("e_segmente", np.concatenate(teile[:-1])))

    for name, audio in varianten:
        pfad = f"{OUT}/{name}.wav"
        sf.write(pfad, audio, rt.sample_rate)
        print(f"  {pfad}  {len(audio)/rt.sample_rate:5.1f} s")

    print(f"""
Anhoeren und vergleichen. Die Frage ist eng: klingen "Pull Request",
"Deployment" und "Staging" englisch oder deutsch?

  a_de           Ist-Zustand
  b_none         ohne Sprach-Tag
  c_en           englisches Tag auf ueberwiegend deutschem Satz
  d_lautschrift  nur fuer MMC brauchbar, dAImon kann Text nicht vorher umschreiben
  e_segmente     korrekt pro Wort, aber mit Nahtstellen in der Prosodie
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
