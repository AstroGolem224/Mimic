"""Entwirft Kandidaten-Stimmen aus Textbeschreibungen. Kein Referenzaudio noetig.

Aufrufweg und Feldnamen stammen aus der Modellkarte von MOSS-VoiceGenerator
(README, Abschnitt "Basic Usage"), nicht aus einer Vermutung. Drei Kandidaten
je Stimme, weil generative Entwuerfe streuen und der erste Wurf selten sitzt.

  uv run python 00_entwerfen.py                     # alle aus stimmen.yaml
  uv run python 00_entwerfen.py bot_kalt            # nur eine daraus
  uv run python 00_entwerfen.py stimmen_wild.yaml   # anderer Satz Entwuerfe
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import soundfile as sf
import torch
import yaml
from transformers import AutoModel, AutoProcessor

from laden import hole, hole_tokenizer

# Der cuDNN-SDPA-Pfad ist laut Modellkarte kaputt. Die drei anderen Backends
# bleiben als Rueckfall an.
torch.backends.cuda.enable_cudnn_sdp(False)
torch.backends.cuda.enable_flash_sdp(True)
torch.backends.cuda.enable_mem_efficient_sdp(True)
torch.backends.cuda.enable_math_sdp(True)

WURZEL = pathlib.Path(__file__).resolve().parent.parent
AUS = WURZEL / "out" / "entwurf"
KANDIDATEN = 3
VERSUCHE = 3

# Aus der Modellkarte uebernommen. Das Modell ist laut eigener Warnung
# empfindlich gegen diese Werte -- nicht ohne Grund daran drehen.
HYPER = dict(
    audio_temperature=1.5,
    audio_top_p=0.6,
    audio_top_k=50,
    audio_repetition_penalty=1.1,
)


def attention_waehlen(geraet: str, dtype: torch.dtype) -> str:
    if geraet == "cuda" and importlib.util.find_spec("flash_attn") and dtype in (torch.float16, torch.bfloat16):
        if torch.cuda.get_device_capability()[0] >= 8:
            return "flash_attention_2"
    return "sdpa" if geraet == "cuda" else "eager"


def main() -> None:
    # Ein Argument ist entweder eine Stimmendatei oder eine einzelne id.
    argument = sys.argv[1] if len(sys.argv) > 1 else None
    quelle = WURZEL / argument if argument and argument.endswith(".yaml") else WURZEL / "stimmen.yaml"
    nur = None if not argument or argument.endswith(".yaml") else argument
    stimmen = yaml.safe_load(quelle.read_text())["stimmen"]
    print(f"[info] {len(stimmen)} Entwuerfe aus {quelle.name}")
    AUS.mkdir(parents=True, exist_ok=True)

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if geraet == "cuda" else torch.float32
    attn = attention_waehlen(geraet, dtype)
    print(f"[info] geraet={geraet} dtype={dtype} attn={attn}")

    pfad = hole("moss_voicegen")
    prozessor = AutoProcessor.from_pretrained(
        pfad,
        trust_remote_code=True,
        normalize_inputs=True,
        codec_path=hole_tokenizer(),
    )
    prozessor.audio_tokenizer = prozessor.audio_tokenizer.to(geraet)
    modell = AutoModel.from_pretrained(
        pfad,
        trust_remote_code=True,
        attn_implementation=attn,
        dtype=dtype,
    ).to(geraet)
    modell.eval()

    rate = prozessor.model_config.sampling_rate

    with torch.no_grad():
        for stimme in stimmen:
            if nur and stimme["id"] != nur:
                continue
            gespraech = [prozessor.build_user_message(text=stimme["text"], instruction=stimme["instruction"])]
            for k in range(KANDIDATEN):
                ziel = AUS / f"{stimme['id']}_{k}.wav"
                if ziel.exists():
                    print(f"{ziel.name}  liegt schon da")
                    continue
                # Der Wurf geht manchmal daneben, auf zwei Arten. Entweder
                # kommt eine Nachricht ohne einen einzigen Audio-Code zurueck,
                # oder der Strom ist so verstuemmelt, dass schon das Zerlegen
                # im Prozessor bricht:
                #
                #   RuntimeError: split_with_sizes expects split_sizes to sum
                #   exactly to 66 (input tensor's size at dimension 0), but got
                #   split_sizes=[60]
                #
                # Beides ist kein Fehler im Aufruf, sondern das Sampling bei
                # temperature 1.5. Also nochmal werfen, hoechstens VERSUCHE mal,
                # dann diesen Kandidaten auslassen.
                wellen = []
                for versuch in range(VERSUCHE):
                    stapel = prozessor([gespraech], mode="generation")
                    ausgabe = modell.generate(
                        input_ids=stapel["input_ids"].to(geraet),
                        attention_mask=stapel["attention_mask"].to(geraet),
                        **HYPER,
                    )
                    try:
                        wellen = [n.audio_codes_list[0] for n in prozessor.decode(ausgabe) if n.audio_codes_list]
                    except RuntimeError as fehler:
                        print(f"{ziel.name}  Wurf zerlegt sich nicht: {fehler}")
                        wellen = []
                    if wellen:
                        break
                    print(f"{ziel.name}  Fehlwurf, Versuch {versuch + 1} von {VERSUCHE}")
                if not wellen:
                    print(f"{ziel.name}  AUSGELASSEN nach {VERSUCHE} leeren Wuerfen")
                    continue
                welle = wellen[0]
                # soundfile statt torchaudio.save: torchaudio 2.9 leitet save() an
                # torchcodec weiter, das hier nicht installiert ist. soundfile ist
                # ohnehin Abhaengigkeit und schreibt dieselbe WAV.
                sf.write(ziel, welle.float().cpu().numpy(), rate)
                print(f"{ziel.name}  {welle.shape[-1] / rate:.1f}s")


if __name__ == "__main__":
    main()
