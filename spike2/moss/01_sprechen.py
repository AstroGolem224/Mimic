"""MOSS-TTS-Local-Transformer-v1.5 (4B) klont eine Referenz und spricht Deutsch.

Das ist die andere Haelfte der Kette. Der Entwerfer (VoiceGenerator, 1.7B) kann
kein Deutsch -- belegt durch die Modellkarte, bestaetigt durch Hoeren. Dieses
Modell kann es: 31 Sprachen, Sprachmarke beim Bauen der Nachricht, natives
48-kHz-Stereo.

Die Frage dieses Laufs: traegt eine **englisch entworfene** Referenz deutschen
Text, wenn nicht dots.tts sondern MOSS selbst spricht? dots.tts hat das mit
hoerbarem englischen Akzent nicht geschafft.

Aufrufweg aus der Modellkarte, Abschnitt "Basic Usage". Zwei Dinge weichen von
VoiceGenerator ab und beide sind Fallen:
  * Tokenizer **v2**, nicht v1. Vertauscht gibt es Rauschen, keinen Fehler.
  * `processor.decode(...)` liefert [Kanaele, Abtastwerte], also stereo.

  uv run python 01_sprechen.py
"""

from __future__ import annotations

import importlib.util
import pathlib

import soundfile as sf
import torch
import yaml
from transformers import AutoModel, AutoProcessor

from laden import hole, hole_tokenizer

# torchaudio 2.9 reicht `load` an torchcodec weiter, und torchcodec kann das
# hier installierte ffmpeg 9 nicht bedienen -- es unterstuetzt 4 bis 8, das
# System hat libavutil.so.61. Der Prozessor liest die Referenz aber ueber
# torchaudio.load. Also wird genau diese Funktion durch soundfile ersetzt:
# eine WAV lesen kann soundfile ohne jede weitere Abhaengigkeit.
import torchaudio  # noqa: E402


def _laden(pfad, *args, **kwargs):
    welle, rate = sf.read(str(pfad), dtype="float32", always_2d=True)
    return torch.from_numpy(welle.T.copy()), rate


torchaudio.load = _laden

torch.backends.cuda.enable_cudnn_sdp(False)
torch.backends.cuda.enable_flash_sdp(True)
torch.backends.cuda.enable_mem_efficient_sdp(True)
torch.backends.cuda.enable_math_sdp(True)

WURZEL = pathlib.Path(__file__).resolve().parent.parent
TEXT = (WURZEL / "referenztext.txt").read_text().strip().replace("\n", " ")
AUS = WURZEL / "out" / "roh" / "moss4b"

# Die Referenzen, die Matthias behalten hat -- englisch entworfen, ungefaerbt.
# Gefaerbte Referenzen scheiden aus: dots.tts lieferte damit dreimal Stille.
REFERENZEN = {
    "sterbende_ki": WURZEL / "out" / "entwurf" / "sterbende_ki_1.wav",
    "drohne": WURZEL / "out" / "entwurf" / "drohne_2.wav",
    # Kontrolle ohne Referenz: so klingt das Modell mit eigener Stimme. Faellt
    # auch die durch, liegt es nicht am Klonen.
    "ohne_referenz": None,
}

# Aus der Modellkarte uebernommen.
HYPER = dict(
    max_new_tokens=4096,
    do_sample=True,
    audio_temperature=1.7,
    audio_top_p=0.8,
    audio_top_k=25,
    audio_repetition_penalty=1.0,
)


def attention_waehlen(geraet: str, dtype: torch.dtype) -> str:
    if geraet == "cuda" and importlib.util.find_spec("flash_attn") and dtype in (torch.float16, torch.bfloat16):
        if torch.cuda.get_device_capability()[0] >= 8:
            return "flash_attention_2"
    return "sdpa" if geraet == "cuda" else "eager"


def main() -> None:
    AUS.mkdir(parents=True, exist_ok=True)
    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if geraet == "cuda" else torch.float32
    print(f"[info] geraet={geraet} attn={attention_waehlen(geraet, dtype)}")

    pfad = hole("moss_local_v15")
    prozessor = AutoProcessor.from_pretrained(
        pfad, trust_remote_code=True, codec_path=hole_tokenizer("v2")
    )
    prozessor.audio_tokenizer = prozessor.audio_tokenizer.to(geraet)
    modell = AutoModel.from_pretrained(
        pfad,
        trust_remote_code=True,
        attn_implementation=attention_waehlen(geraet, dtype),
        dtype=dtype,
    ).to(geraet)
    modell.eval()

    rate = prozessor.model_config.sampling_rate

    with torch.no_grad():
        for name, referenz in REFERENZEN.items():
            bauteile = dict(text=TEXT, language="German")
            if referenz is not None:
                bauteile["reference"] = [str(referenz)]
            gespraech = [prozessor.build_user_message(**bauteile)]

            stapel = prozessor([gespraech], mode="generation")
            ausgabe = modell.generate(
                input_ids=stapel["input_ids"].to(geraet),
                attention_mask=stapel["attention_mask"].to(geraet),
                **HYPER,
            )
            for nachricht in prozessor.decode(ausgabe):
                if nachricht is None or not nachricht.audio_codes_list:
                    print(f"{name}  leere Ausgabe")
                    continue
                welle = nachricht.audio_codes_list[0].float().cpu().numpy()
                # [Kanaele, Abtastwerte] -> soundfile will [Abtastwerte, Kanaele]
                if welle.ndim == 2:
                    welle = welle.T
                ziel = AUS / f"{name}__t7.wav"
                sf.write(ziel, welle, rate)
                print(f"{ziel.name}  {welle.shape[0] / rate:.1f}s  {rate} Hz")


if __name__ == "__main__":
    main()
