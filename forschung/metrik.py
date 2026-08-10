# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["resemblyzer", "numpy<2", "setuptools<81"]  # webrtcvad importiert pkg_resources; ab setuptools 81 entfernt
# ///
"""Sprecher-Aehnlichkeit zwischen WAV-Dateien -- Arbeitspaket 1 der
autoresearch-Adaption (siehe UMBRA-Notes/DDs/Mimic/autoresearch-adaption.md).

Aufruf:  uv run forschung/metrik.py REFERENZ.wav PROBE.wav [PROBE2.wav ...]
Ausgabe: eine Zeile je Probe -- Kosinus zwischen den Speaker-Embeddings
(Resemblyzer/GE2E, 256 Dimensionen). Hoeher = aehnlicher zur Referenz.

Die Schwelle, ab der eine Probe als "verfaelscht" gilt, wird HIER gemessen
und nicht geraten: gleiche Stimme gegen fremde Stimme muss klar trennen,
sonst taugt die Metrik nicht als Zielfunktion fuer die Experimentierschleife.
"""

import sys

import numpy as np
from resemblyzer import VoiceEncoder, preprocess_wav


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    encoder = VoiceEncoder(verbose=False)
    referenz = encoder.embed_utterance(preprocess_wav(sys.argv[1]))
    for pfad in sys.argv[2:]:
        embedding = encoder.embed_utterance(preprocess_wav(pfad))
        wert = float(np.dot(referenz, embedding))   # Embeddings sind L2-normiert
        print(f"{wert:.4f}  {pfad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
