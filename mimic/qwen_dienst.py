"""Dauerhaft warmer Qwen-Klonprozess fuer den Mimic-Worker.

Laeuft in ``entwurf-venv-qwen-klon`` und importiert deshalb nichts aus dem
Mimic-Paket. stdin/stdout tragen JSON-Zeilen; Audio landet als kurzlebige WAV
im vom Aufrufer vorgegebenen Laufzeitverzeichnis.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import pathlib
import signal
import sys

MODELL_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
MODELL_REVISION = "fd4b254389122332181a7c3db7f27e918eec64e3"
PROTOKOLL = sys.stdout


def melde(**felder: object) -> None:
    print(json.dumps(felder, ensure_ascii=False), file=PROTOKOLL, flush=True)


def elterntod() -> None:
    """Linux: kein mehrere GB grosser GPU-Waise nach einem Worker-Neustart."""
    try:
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (AttributeError, OSError):
        pass


def sprache_waehlen(modell) -> str:
    hole = getattr(modell.model, "get_supported_languages", None)
    for kandidat in list(hole()) if callable(hole) else []:
        if str(kandidat).lower().startswith(("german", "de")):
            return kandidat
    raise RuntimeError("Qwen meldet keine deutsche Sprache")


def main() -> int:
    elterntod()
    # Fremdbibliotheken schreiben beim Laden und Generieren teils komplette
    # Nutztexte. stdout ist zugleich unser IPC-Kanal, stderr wuerde durch den
    # Parent im Journal landen. Nur `melde()` schreibt gezielt ins Protokoll.
    still = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = still
    sys.stderr = still
    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from qwen_tts import Qwen3TTSModel

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if geraet == "cuda" else torch.float32
    pfad = snapshot_download(repo_id=MODELL_REPO, revision=MODELL_REVISION,
                             local_files_only=True)
    modell = Qwen3TTSModel.from_pretrained(
        pfad, device_map=geraet, torch_dtype=dtype,
        attn_implementation="sdpa" if geraet == "cuda" else "eager")
    sprache = sprache_waehlen(modell)
    rate = 24_000
    melde(kind="bereit", rate=rate, geraet=geraet, sprache=str(sprache))

    prompts: dict[tuple[str, int, int], object] = {}
    for zeile in sys.stdin:
        auftrag: dict = {}
        try:
            auftrag = json.loads(zeile)
            quelle = pathlib.Path(auftrag["quelle"])
            ziel = pathlib.Path(auftrag["aus"])
            info = quelle.stat()
            schluessel = (str(quelle), info.st_mtime_ns, info.st_size)
            prompt = prompts.get(schluessel)
            if prompt is None:
                prompt = modell.create_voice_clone_prompt(
                    ref_audio=str(quelle), ref_text=None, x_vector_only_mode=True)
                prompts = {schluessel: prompt}
            text = " ".join(str(auftrag["text"]).split())
            saat = int.from_bytes(hashlib.sha256(
                (str(quelle) + "\0" + text).encode()).digest()[:8], "big") & 0x7fffffff
            torch.manual_seed(saat)
            if geraet == "cuda":
                torch.cuda.manual_seed_all(saat)
            wellen, aus_rate = modell.generate_voice_clone(
                text=text, language=sprache, voice_clone_prompt=prompt,
                do_sample=True, temperature=0.8, top_p=0.95, top_k=50,
                repetition_penalty=1.05, max_new_tokens=2048)
            if not wellen:
                raise RuntimeError("leere Ausgabe")
            ziel.parent.mkdir(parents=True, exist_ok=True)
            sf.write(ziel, wellen[0], aus_rate, subtype="PCM_16")
            os.chmod(ziel, 0o600)
            melde(kind="fertig", id=auftrag.get("id"), aus=str(ziel),
                  rate=int(aus_rate), samples=len(wellen[0]))
        except Exception as fehler:  # noqa: BLE001 -- Fehler ist Teil des Protokolls
            melde(kind="fehler", id=auftrag.get("id"),
                  grund=f"{type(fehler).__name__}: Qwen-Generierung fehlgeschlagen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
