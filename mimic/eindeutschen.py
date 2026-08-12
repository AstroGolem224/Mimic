"""Laesst ein entworfenes Timbre Deutsch sprechen. Laeuft in einer FREMDEN Umgebung.

Gegenstueck zu `entwerfen.py` und derselbe Bau: dieses Modul wird NICHT von
Mimic importiert, sondern von `mimic/entwurf.py` als Skript in der
Entwurfs-venv gestartet. Also **kein Import aus dem Paket `mimic`**, nur
Standardbibliothek plus torch/transformers/soundfile.

  python -m eindeutschen '{"referenz": "/tmp/k0.wav", "text": "...", "aus": "/tmp/de.wav"}'

## Wozu dieser Schritt ueberhaupt

MOSS-VoiceGenerator kann kein Deutsch. Die Modellkarte nennt Chinesisch und
Englisch, und am 2026-08-11 ist es auch gehoert worden: zwoelf Kandidaten mit
deutschem Text, alle durchgefallen, auch mit deutschen Anweisungen. Ein
englischer Entwurf direkt als `ref.wav` schleppt den Akzent in jeden spaeteren
deutschen Satz -- dots.tts hat keine Sprachmarke, es klont, was es hoert.

MOSS-TTS-Local-Transformer-v1.5 (4B) hat eine: 31 Sprachen, `language="German"`
beim Bauen der Nachricht. Es behaelt die Stimmfarbe der Referenz und spricht
sauberes Deutsch. Erst dessen Ausgabe taugt als `ref.wav`.

Belegt in `spike2/ERGEBNIS.md`, Kriterium C: dots.tts durchgefallen, MOSS 4B
bestanden, gleiche Referenzen, gleicher Text.

## Zwei Fallen, beide teuer gewesen

* **Tokenizer v2, nicht v1.** Das 4B nutzt MOSS-Audio-Tokenizer-v2 (48 kHz
  stereo), VoiceGenerator den v1 (24 kHz mono). Vertauscht gibt es Rauschen,
  keine Fehlermeldung.
* **`torchaudio.load` ist hier unbrauchbar.** torchaudio 2.9 reicht es an
  torchcodec weiter, das ffmpeg 4 bis 8 unterstuetzt; auf diesem System liegt
  ffmpeg 9 (`libavutil.so.61`). Der Prozessor liest die Referenz aber genau
  darueber. Also wird die Funktion durch soundfile ersetzt -- eine WAV lesen
  kann das ohne jede weitere Abhaengigkeit.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

# Feste Revisionen, ermittelt 2026-08-11 ueber die HF-API. Dieselben wie in
# spike2/revisions.yaml -- dort dokumentiert, hier dupliziert, weil spike2/
# Wegwerfcode ist und die App nicht davon abhaengen darf.
MODELL_REPO = "OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5"
MODELL_REVISION = "be7766a6735b98bd793f7c79fb720b4d0f5d13b8"
TOKENIZER_REPO = "OpenMOSS-Team/MOSS-Audio-Tokenizer-v2"
TOKENIZER_REVISION = "f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169"

# Aus der Modellkarte uebernommen, Abschnitt "Basic Usage".
HYPER = dict(
    max_new_tokens=4096,
    do_sample=True,
    audio_temperature=1.7,
    audio_top_p=0.8,
    audio_top_k=25,
    audio_repetition_penalty=1.0,
)


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
    referenz = pathlib.Path(auftrag["referenz"])
    text = auftrag["text"]
    aus = pathlib.Path(auftrag["aus"])
    aus.parent.mkdir(parents=True, exist_ok=True)
    if not referenz.is_file():
        melde(kind="fehler", grund=f"Referenz {referenz} gibt es nicht")
        return 1

    import soundfile as sf
    import torch
    import torchaudio
    from huggingface_hub import snapshot_download
    from transformers import AutoModel, AutoProcessor

    def _laden(pfad, *_args, **_kwargs):
        welle, rate = sf.read(str(pfad), dtype="float32", always_2d=True)
        return torch.from_numpy(welle.T.copy()), rate

    torchaudio.load = _laden

    # Der cuDNN-SDPA-Pfad ist laut Modellkarte kaputt. Die drei anderen bleiben.
    torch.backends.cuda.enable_cudnn_sdp(False)
    torch.backends.cuda.enable_flash_sdp(True)
    torch.backends.cuda.enable_mem_efficient_sdp(True)
    torch.backends.cuda.enable_math_sdp(True)

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if geraet == "cuda" else torch.float32

    melde(kind="laden", geraet=geraet)
    # Beide Repos vorher holen und nur lokale Pfade uebergeben: der Prozessor
    # reicht sein gesamtes kwargs an den Audio-Tokenizer weiter, der in einem
    # ANDEREN Repo liegt. Ein `revision=` traefe dort auf nichts.
    pfad = snapshot_download(repo_id=MODELL_REPO, revision=MODELL_REVISION)
    codec = snapshot_download(repo_id=TOKENIZER_REPO, revision=TOKENIZER_REVISION)
    prozessor = AutoProcessor.from_pretrained(pfad, trust_remote_code=True, codec_path=codec)
    prozessor.audio_tokenizer = prozessor.audio_tokenizer.to(geraet)
    modell = AutoModel.from_pretrained(
        pfad, trust_remote_code=True,
        attn_implementation=attention_waehlen(geraet, dtype), dtype=dtype).to(geraet)
    modell.eval()
    rate = prozessor.model_config.sampling_rate
    melde(kind="bereit")

    gespraech = [prozessor.build_user_message(
        text=text, reference=[str(referenz)], language="German")]
    with torch.no_grad():
        stapel = prozessor([gespraech], mode="generation")
        ausgabe = modell.generate(
            input_ids=stapel["input_ids"].to(geraet),
            attention_mask=stapel["attention_mask"].to(geraet),
            **HYPER,
        )
        for nachricht in prozessor.decode(ausgabe):
            if nachricht is None or not nachricht.audio_codes_list:
                continue
            welle = nachricht.audio_codes_list[0].float().cpu().numpy()
            # Der 48-kHz-Codec liefert [Kanaele, Abtastwerte], soundfile will
            # es andersherum.
            if welle.ndim == 2:
                welle = welle.T
            sf.write(aus, welle, rate)
            melde(kind="fertig", datei=str(aus), dauer=round(welle.shape[0] / rate, 1), rate=rate)
            return 0

    melde(kind="fehler", grund="leere Ausgabe")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as fehler:            # noqa: BLE001 -- der Aufrufer braucht den Grund, nicht den Stack
        melde(kind="fehler", grund=f"{type(fehler).__name__}: {fehler}"[:300])
        sys.exit(1)
