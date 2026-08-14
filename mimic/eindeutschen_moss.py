"""Nimmt einer fremden Aufnahme den Akzent. Laeuft in einer FREMDEN Umgebung.

Anderer Auftrag als die `entwerfen_*.py`: die entwerfen eine Stimme aus einer
Beschreibung, das hier **klont eine vorhandene** und laesst sie Deutsch
sprechen. Gleiches Protokoll, eine JSON-Zeile je Ereignis.

**Kein Import aus dem Paket `mimic`**, ein Test wacht darueber.

  python eindeutschen_moss.py '{"quelle":"/pfad/stimme.mp3","text":"...","aus":"/tmp/de.wav"}'

## Wofuer

Eine gekaufte oder anderswo erzeugte Stimme spricht Deutsch oft mit fremdem
Einschlag -- gerolltes R, englische Vokale. dots.tts kann das nicht abstellen:
es hat keine Sprachmarke, es klont was es hoert, Akzent inklusive.

MOSS-TTS-Local-Transformer-v1.5 hat eine. Es hoert die Aufnahme, behaelt die
Stimmfarbe und spricht den Text mit `language="German"` neu. Dessen Ausgabe
wird die eigentliche Referenz.

Gemessen am 2026-08-12 und 2026-08-14 an vier Stimmen (ether, geth, forge
zweimal): der Akzent verschwindet hoerbar, die Stimme bleibt erkennbar. Bei
`ether` stand die Alternative daneben -- den englischen Mittelteil der
Aufnahme wegzuschneiden --, und dieser Weg hat gewonnen.

## Zwei Fallen, beide teuer gewesen

* **Tokenizer v2, nicht v1.** Das 4B nutzt MOSS-Audio-Tokenizer-v2 (48 kHz
  stereo). Mit dem v1 gibt es Rauschen und keine Fehlermeldung.
* **`torchaudio.load` ist hier unbrauchbar.** torchaudio 2.9 reicht es an
  torchcodec weiter, das ffmpeg 4 bis 8 kennt; hier liegt ffmpeg 9
  (`libavutil.so.61`). Der Prozessor liest die Referenz aber genau darueber,
  also wird die Funktion durch soundfile ersetzt.

## Was es nicht kann

Die Dauer steuert das Modell nicht. Derselbe Text kam als 8.0 s und als 23.0 s
zurueck, je nach Sprechtempo der Vorlage. dots.tts klont am besten aus 8 bis
15 Sekunden; aus einer 23-Sekunden-Referenz wurde Gebrumm. Wer eine Laenge
braucht, wirft mehrfach -- der Aufrufer entscheidet das, nicht dieses Skript.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Feste Revisionen, ermittelt 2026-08-11 ueber die HF-API. Siehe
# spike2/revisions.yaml, wo auch steht, wie sie ermittelt wurden.
MODELL_REPO = "OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5"
MODELL_REVISION = "be7766a6735b98bd793f7c79fb720b4d0f5d13b8"
TOKENIZER_REPO = "OpenMOSS-Team/MOSS-Audio-Tokenizer-v2"
TOKENIZER_REVISION = "f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169"

# Aus der Modellkarte, Abschnitt "Basic Usage".
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


def main() -> int:
    auftrag = json.loads(sys.argv[1])
    quelle = pathlib.Path(auftrag["quelle"]).expanduser()
    text = " ".join(str(auftrag["text"]).split())
    aus = pathlib.Path(auftrag["aus"])
    aus.parent.mkdir(parents=True, exist_ok=True)
    if not quelle.is_file():
        melde(kind="fehler", grund=f"{quelle} gibt es nicht")
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
    # ANDEREN Repo liegt. Ein `revision=` traefe dort auf nichts und endete in
    # einem "Unrecognized model"-Fehler, der wie ein kaputtes Repo aussieht.
    pfad = snapshot_download(repo_id=MODELL_REPO, revision=MODELL_REVISION)
    codec = snapshot_download(repo_id=TOKENIZER_REPO, revision=TOKENIZER_REVISION)
    prozessor = AutoProcessor.from_pretrained(pfad, trust_remote_code=True, codec_path=codec)
    prozessor.audio_tokenizer = prozessor.audio_tokenizer.to(geraet)
    modell = AutoModel.from_pretrained(
        pfad, trust_remote_code=True,
        attn_implementation="sdpa" if geraet == "cuda" else "eager", dtype=dtype).to(geraet)
    modell.eval()
    rate = prozessor.model_config.sampling_rate
    melde(kind="bereit", rate=rate)

    gespraech = [prozessor.build_user_message(
        text=text, reference=[str(quelle)], language="German")]
    stapel = prozessor([gespraech], mode="generation")
    with torch.no_grad():
        ausgabe = modell.generate(
            input_ids=stapel["input_ids"].to(geraet),
            attention_mask=stapel["attention_mask"].to(geraet),
            **HYPER,
        )
    for nachricht in prozessor.decode(ausgabe):
        if nachricht is None or not nachricht.audio_codes_list:
            continue
        welle = nachricht.audio_codes_list[0].float().cpu().numpy()
        # Der 48-kHz-Codec liefert [Kanaele, Abtastwerte], soundfile will es
        # andersherum.
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
