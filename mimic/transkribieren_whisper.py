"""Fremdumgebungs-Skript fuer faster-whisper; importiert absichtlich kein mimic."""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        auftrag = json.loads(sys.argv[1])
        from faster_whisper import WhisperModel

        geraet = auftrag.get("geraet", "cpu")
        modell = WhisperModel(auftrag.get("modell", "small"), device=geraet,
                              compute_type="int8" if geraet == "cpu" else "default")
        segmente, info = modell.transcribe(
            auftrag["quelle"], beam_size=5, vad_filter=True,
            condition_on_previous_text=False)
        text = " ".join(segment.text.strip() for segment in segmente if segment.text.strip())
        print(json.dumps({"text": text, "sprache": info.language,
                          "wahrscheinlichkeit": info.language_probability},
                         ensure_ascii=False), flush=True)
        return 0
    except Exception as fehler:
        print(json.dumps({"fehler": f"{type(fehler).__name__}: {fehler}"},
                         ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
