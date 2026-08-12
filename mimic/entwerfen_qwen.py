"""Entwirft Stimmen mit Qwen3-TTS-VoiceDesign. Laeuft in einer FREMDEN Umgebung.

Gegenstueck zu entwerfen_voxcpm.py, gleiches Protokoll, andere Staerken:
Qwens Deutsch war am 2026-08-12 fehlerfrei, wo VoxCPM2 einzelne Woerter
englisch las. Dafuer liefert es 24 kHz statt 48 -- als ref.wav also halbe
Bandbreite, die kein Hochrechnen zurueckholt.

**Kein Import aus dem Paket `mimic`**, ein Test wacht darueber.

  python entwerfen_qwen.py '{"instruction":"...","text":"...","anzahl":3,"aus":"/tmp/x"}'

VoiceDesign entwirft aus der Beschreibung, statt aus Referenzaudio zu klonen.
Damit gibt es keinen Weg, ueber den ein fremder Akzent einsickern koennte --
daran ist die erste Entwurfskette ueber MOSS gescheitert.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Feste Revision, ermittelt 2026-08-12. Siehe spike2/revisions.yaml.
MODELL_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
MODELL_REVISION = "5ecdb67327fd37bb2e042aab12ff7391903235d3"


def melde(**felder: object) -> None:
    print(json.dumps(felder, ensure_ascii=False), flush=True)


def sprache_waehlen(modell) -> str:
    """Deutsch so nennen, wie das Modell es nennt, statt zu raten.

    qwen_tts prueft gegen `get_supported_languages()` und wirft sonst einen
    ValueError. Die Schreibweise ("german", "German", "de") ist nicht
    dokumentiert, also wird sie erfragt.
    """
    hole = getattr(modell.model, "get_supported_languages", None)
    unterstuetzt = list(hole()) if callable(hole) else []
    for kandidat in unterstuetzt:
        if str(kandidat).lower().startswith(("german", "de")):
            return kandidat
    raise RuntimeError(f"kein Deutsch in {sorted(unterstuetzt)}")


def main() -> int:
    auftrag = json.loads(sys.argv[1])
    beschreibung = auftrag["instruction"]
    text = auftrag["text"]
    anzahl = int(auftrag.get("anzahl", 3))
    aus = pathlib.Path(auftrag["aus"])
    aus.mkdir(parents=True, exist_ok=True)

    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from qwen_tts import Qwen3TTSModel

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    melde(kind="laden", geraet=geraet)
    pfad = snapshot_download(repo_id=MODELL_REPO, revision=MODELL_REVISION)
    # bf16 direkt aufs Geraet: der Umweg ueber fp32 auf der CPU zieht laut
    # Phase-0-Messung ein Mehrfaches an RAM und kann den Prozess killen.
    modell = Qwen3TTSModel.from_pretrained(
        pfad, device_map=geraet,
        torch_dtype=torch.bfloat16 if geraet == "cuda" else torch.float32)
    sprache = sprache_waehlen(modell)
    melde(kind="bereit", sprache=sprache)

    for k in range(anzahl):
        try:
            wellen, rate = modell.generate_voice_design(
                text=text, instruct=beschreibung, language=sprache)
        except Exception as fehler:          # noqa: BLE001 -- ein Fehlwurf killt nicht den Lauf
            melde(kind="fehlwurf", nummer=k, grund=f"{type(fehler).__name__}: {fehler}"[:150])
            continue
        ziel = aus / f"kandidat_{k}.wav"
        sf.write(ziel, wellen[0], rate)
        melde(kind="kandidat", nummer=k, datei=str(ziel),
              dauer=round(len(wellen[0]) / rate, 1))
    melde(kind="fertig")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as fehler:            # noqa: BLE001 -- die GUI braucht den Grund, nicht den Stack
        melde(kind="fehler", grund=f"{type(fehler).__name__}: {fehler}"[:300])
        sys.exit(1)
