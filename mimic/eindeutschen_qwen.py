"""Eindeutschen v2: nur die Sprecheridentitaet aus der Vorlage uebernehmen.

MOSS v1 reicht die komplette englische Aufnahme als In-Context-Audio an das
Modell. Das erhaelt die Stimme gut, kann aber auch englische Phonetik, Rhythmus
und Akzent in die deutsche Ausgabe ziehen. Qwen3-TTS Base kann den Prompt auf
den x-vector (Sprecher-Embedding) reduzieren. Der deutsche Text wird damit
nativ erzeugt, ohne die englischen Audio-Codes als Aussprachevorlage.

Das Skript laeuft absichtlich in einer eigenen Umgebung und importiert nichts
aus ``mimic``. Protokoll: eine JSON-Zeile je Ereignis.
"""

from __future__ import annotations

import json
import pathlib
import sys

MODELL_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
MODELL_REVISION = "fd4b254389122332181a7c3db7f27e918eec64e3"


def melde(**felder: object) -> None:
    print(json.dumps(felder, ensure_ascii=False), flush=True)


def sprache_waehlen(modell) -> str:
    hole = getattr(modell.model, "get_supported_languages", None)
    unterstuetzt = list(hole()) if callable(hole) else []
    for kandidat in unterstuetzt:
        if str(kandidat).lower().startswith(("german", "de")):
            return kandidat
    raise RuntimeError(f"kein Deutsch in {sorted(unterstuetzt)}")


def main() -> int:
    auftrag = json.loads(sys.argv[1])
    quelle = pathlib.Path(auftrag["quelle"]).expanduser()
    text = " ".join(str(auftrag["text"]).split())
    aus = pathlib.Path(auftrag["aus"])
    saat = int(auftrag.get("saat", 20260818))
    aus.parent.mkdir(parents=True, exist_ok=True)
    if not quelle.is_file():
        melde(kind="fehler", grund=f"{quelle} gibt es nicht")
        return 1

    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from qwen_tts import Qwen3TTSModel

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if geraet == "cuda" else torch.float32
    melde(kind="laden", geraet=geraet)
    pfad = snapshot_download(repo_id=MODELL_REPO, revision=MODELL_REVISION)
    modell = Qwen3TTSModel.from_pretrained(
        pfad, device_map=geraet, torch_dtype=dtype,
        attn_implementation="sdpa" if geraet == "cuda" else "eager")
    sprache = sprache_waehlen(modell)
    melde(kind="bereit", sprache=str(sprache))

    # Der entscheidende Unterschied zu v1: kein ref_text und keine Audio-Codes
    # der englischen Vorlage. Nur das Sprecher-Embedding wird konditioniert.
    prompt = modell.create_voice_clone_prompt(
        ref_audio=str(quelle), ref_text=None, x_vector_only_mode=True)
    torch.manual_seed(saat)
    if geraet == "cuda":
        torch.cuda.manual_seed_all(saat)
    wavs, rate = modell.generate_voice_clone(
        text=text,
        language=sprache,
        voice_clone_prompt=prompt,
        do_sample=True,
        temperature=0.8,
        top_p=0.95,
        top_k=50,
        repetition_penalty=1.05,
        max_new_tokens=2048,
    )
    if not wavs:
        melde(kind="fehler", grund="leere Ausgabe")
        return 1
    welle = wavs[0]
    sf.write(aus, welle, rate)
    dauer = len(welle) / rate
    melde(kind="fertig", datei=str(aus), dauer=round(dauer, 1), rate=rate,
          modus="x-vector-only", saat=saat)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as fehler:  # noqa: BLE001 -- JSON-Fehler gehoert zum Protokoll
        melde(kind="fehler", grund=f"{type(fehler).__name__}: {fehler}"[:300])
        sys.exit(1)
