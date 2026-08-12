"""Entwirft Stimmen aus einer Beschreibung. Laeuft in einer FREMDEN Umgebung.

Dieses Modul wird NICHT von Mimic importiert. Es wird von `mimic/entwurf.py`
als Skript in einer eigenen venv gestartet, weil MOSS-VoiceGenerator
transformers 5.0.0 verlangt und der Worker auf 4.57.6 mit dots.tts sitzt.
Zwei transformers in einem Prozess gibt es nicht.

Daraus folgt die Regel fuer diese Datei: **kein Import aus dem Paket `mimic`**.
Nur Standardbibliothek plus torch/transformers/soundfile. Alles, was hier steht,
muss auch ohne den Rest von Mimic laufen.

Der Aufrufweg stammt aus der Modellkarte von MOSS-VoiceGenerator und aus dem
gemessenen Spike (spike2/moss/00_entwerfen.py) -- nicht aus einer Vermutung.

  python -m entwerfen '{"instruction": "...", "text": "...", "anzahl": 3, "aus": "/tmp/x"}'

Ausgabe: eine JSON-Zeile je Ereignis auf stdout, damit die GUI mitlesen kann,
statt am Ende einen Klumpen zu bekommen.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

# Feste Revisionen, ermittelt 2026-08-11. Dieselben wie in spike2/revisions.yaml
# -- dort dokumentiert, hier dupliziert, weil spike2/ laut README Wegwerfcode
# ist und die App nicht davon abhaengen darf.
MODELL_REPO = "OpenMOSS-Team/MOSS-VoiceGenerator"
MODELL_REVISION = "97521ec2b6f3ec5026ac1f5751f8fc302d82c2d4"
TOKENIZER_REPO = "OpenMOSS-Team/MOSS-Audio-Tokenizer"
TOKENIZER_REVISION = "3cd226ba2947efa357ef453bcad111b6eafba782"

# Aus der Modellkarte uebernommen. Das Modell ist laut eigener Warnung
# empfindlich gegen diese Werte -- nicht ohne Messung daran drehen.
HYPER = dict(
    audio_temperature=1.5,
    audio_top_p=0.6,
    audio_top_k=50,
    audio_repetition_penalty=1.1,
)
# Bei temperature 1.5 geht ein Wurf regelmaessig daneben: entweder kommt eine
# Nachricht ohne einen einzigen Audio-Code zurueck, oder der Strom ist so
# verstuemmelt, dass schon das Zerlegen im Prozessor bricht. Beides ist kein
# Fehler im Aufruf, sondern das Sampling. Also nochmal werfen.
VERSUCHE = 3


def melde(**felder: object) -> None:
    print(json.dumps(felder, ensure_ascii=False), flush=True)


def attention_waehlen(geraet: str, dtype) -> str:
    import torch

    if geraet == "cuda" and importlib.util.find_spec("flash_attn") and dtype in (
            torch.float16, torch.bfloat16):
        if torch.cuda.get_device_capability()[0] >= 8:
            return "flash_attention_2"
    return "sdpa" if geraet == "cuda" else "eager"


def main() -> int:
    auftrag = json.loads(sys.argv[1])
    instruction = auftrag["instruction"]
    text = auftrag["text"]
    anzahl = int(auftrag.get("anzahl", 3))
    aus = pathlib.Path(auftrag["aus"])
    aus.mkdir(parents=True, exist_ok=True)

    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModel, AutoProcessor

    # Der cuDNN-SDPA-Pfad ist laut Modellkarte kaputt. Die drei anderen bleiben.
    torch.backends.cuda.enable_cudnn_sdp(False)
    torch.backends.cuda.enable_flash_sdp(True)
    torch.backends.cuda.enable_mem_efficient_sdp(True)
    torch.backends.cuda.enable_math_sdp(True)

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if geraet == "cuda" else torch.float32

    melde(kind="laden", geraet=geraet)
    # Beide Repos vorher auf ihre Revision holen und dem Prozessor nur lokale
    # Pfade geben: `MossTTSProcessor.from_pretrained` reicht sein gesamtes
    # kwargs an den Audio-Tokenizer weiter, der in einem ANDEREN Repo liegt.
    # Ein `revision=` traefe dort auf nichts und endete in einem
    # "Unrecognized model"-Fehler, der wie ein kaputtes Repo aussieht.
    pfad = snapshot_download(repo_id=MODELL_REPO, revision=MODELL_REVISION)
    codec = snapshot_download(repo_id=TOKENIZER_REPO, revision=TOKENIZER_REVISION)
    prozessor = AutoProcessor.from_pretrained(
        pfad, trust_remote_code=True, normalize_inputs=True, codec_path=codec)
    prozessor.audio_tokenizer = prozessor.audio_tokenizer.to(geraet)
    modell = AutoModel.from_pretrained(
        pfad, trust_remote_code=True,
        attn_implementation=attention_waehlen(geraet, dtype), dtype=dtype).to(geraet)
    modell.eval()
    rate = prozessor.model_config.sampling_rate
    melde(kind="bereit")

    gespraech = [prozessor.build_user_message(text=text, instruction=instruction)]
    with torch.no_grad():
        for k in range(anzahl):
            ziel = aus / f"kandidat_{k}.wav"
            wellen = []
            for versuch in range(VERSUCHE):
                stapel = prozessor([gespraech], mode="generation")
                ausgabe = modell.generate(
                    input_ids=stapel["input_ids"].to(geraet),
                    attention_mask=stapel["attention_mask"].to(geraet),
                    **HYPER,
                )
                try:
                    wellen = [n.audio_codes_list[0] for n in prozessor.decode(ausgabe)
                              if n.audio_codes_list]
                except RuntimeError as fehler:
                    melde(kind="fehlwurf", nummer=k, versuch=versuch + 1, grund=str(fehler)[:120])
                    wellen = []
                if wellen:
                    break
                melde(kind="fehlwurf", nummer=k, versuch=versuch + 1, grund="kein Audio")
            if not wellen:
                melde(kind="ausgelassen", nummer=k)
                continue
            welle = wellen[0]
            # soundfile statt torchaudio.save: torchaudio 2.9 leitet save() an
            # torchcodec weiter, das hier nicht installiert ist.
            sf.write(ziel, welle.float().cpu().numpy(), rate)
            melde(kind="kandidat", nummer=k, datei=str(ziel),
                  dauer=round(welle.shape[-1] / rate, 1))
    melde(kind="fertig")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as fehler:            # noqa: BLE001 -- die GUI braucht den Grund, nicht den Stack
        melde(kind="fehler", grund=f"{type(fehler).__name__}: {fehler}"[:300])
        sys.exit(1)
