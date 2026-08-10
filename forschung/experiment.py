"""Ein Experimentlauf: Korpus synthetisieren, Sprecher-Aehnlichkeit messen.

DIESE DATEI EDITIERT DER AGENT -- aber nur den Stellschrauben-Block unten.
Alles darunter ist Messapparat; wer den Apparat aendert, macht die Journale
unvergleichbar. Ablauf und Regeln stehen in program.md.

Die Synthese laeuft bewusst IM PROZESS statt ueber /speak: die Stellschrauben
MIN/MAX_SATZ_ZEICHEN sind Modulkonstanten des Dienstes und ueber die
Schnittstelle nicht je Anfrage stellbar. Es werden dieselben Funktionen
benutzt wie im Worker (split_sentences, tensor_to_pcm, STUMM_PEAK), ein
Gewinner wird danach als regulaerer Commit uebernommen und einmal ueber
/speak gegengehoert.
"""

from __future__ import annotations

# ── Stellschrauben (NUR diesen Block editieren) ─────────────────────────
MIN_SATZ_ZEICHEN = 20      # Vorgabe des Dienstes: 20
MAX_SATZ_ZEICHEN = 80      # Vorgabe des Dienstes: 80 (seit Nachtlauf 2026-08-10)
PAUSE_MS = 180             # Atempause zwischen Saetzen, Vorgabe: 180
SPEAKER_SCALE = 1.0        # None = Wert aus dem Stimmprofil (Profile stehen auf 1.0)
NOTIZ = "gewinnerlage: max_satz 80, speaker_scale 1.0 (zweifach bestaetigt)"
# ────────────────────────────────────────────────────────────────────────

BUDGET_S = 300.0           # Zeitbudget wie im autoresearch-Original
MAX_VERSUCHE = 2           # stummer Take -> genau eine Wiederholung, wie im Worker
# WER-Waechter: eine Probe ueber dieser Wortfehlerrate macht den ganzen Lauf
# ungueltig -- Aehnlichkeit, die durch geopferte Sprache gewonnen wird, zaehlt
# nicht. Gemessen an zwei Baseline-Laeufen am 2026-08-10 (whisper small, int8,
# CPU): intakte Proben lagen bei 0.00-0.11, Proben mit stochastisch
# verschlucktem Chunk bei 0.31 und 0.50 -- die Aehnlichkeit blieb dabei
# unauffaellig (0.86-0.88). 0.25 trennt beide Gruppen mit Abstand.
WER_DECKEL = 0.25
WHISPER_MODELL = "small"   # mehrsprachig; base war bei deutschen Umlauten unsicher

import dataclasses
import json
import os
import time
import wave
from pathlib import Path

from prepare import KORPUS, cache_pfad

FORSCHUNG = Path(__file__).parent
JOURNAL = FORSCHUNG / "journal.jsonl"


def synthese(runtime, profil, text: str) -> bytes:
    """PCM fuer einen Korpustext, satzweise wie im Worker, mit Stumm-Wiederholung.

    ponytail: kein Praefix-Stille-Beschnitt wie im Worker -- die Aehnlichkeits-
    metrik hoert ueber Stille hinweg; Beschnitt kaeme erst, wenn Messwerte
    dadurch nachweislich verzerrt werden.
    """
    from mimic import voices
    from mimic.worker import STUMM_PEAK, peak_int16, tensor_to_pcm

    saetze = voices.split_sentences(text) or [text]
    pause = bytes(int(runtime.sample_rate * PAUSE_MS / 1000) * 2)
    teile: list[bytes] = []
    for index, satz in enumerate(saetze):
        if index:
            teile.append(pause)
        for versuch in range(MAX_VERSUCHE):
            take = b"".join(
                tensor_to_pcm(chunk, profil.gain)
                for chunk in runtime.generate_stream(
                    text=satz, language=profil.language,
                    speaker_scale=profil.speaker_scale,
                    prompt_audio_path=profil.wav_path,
                    prompt_text=profil.prompt_text))
            if peak_int16(take) > STUMM_PEAK:
                break
        teile.append(take)
    return b"".join(teile)


_UMSCHRIFT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _woerter(text: str) -> list[str]:
    """Korpustexte stehen in ASCII-Umschrift, Whisper liefert echte Umlaute --
    beide Seiten auf dieselbe Form bringen, sonst zaehlt jedes Gemaeuer als
    Fehler."""
    import re
    text = text.lower().translate(_UMSCHRIFT)
    return re.findall(r"[a-z0-9]+", text)


def wer(soll: str, ist: str) -> float:
    """Wortfehlerrate ueber Levenshtein-Distanz. Eigene 15 Zeilen statt jiwer:
    eine Abhaengigkeit weniger im Overlay, und die Definition steht lesbar da."""
    a, b = _woerter(soll), _woerter(ist)
    if not a:
        return 0.0
    zeile = list(range(len(b) + 1))
    for i, wort_a in enumerate(a, 1):
        vorher, zeile[0] = zeile[0], i
        for j, wort_b in enumerate(b, 1):
            vorher, zeile[j] = zeile[j], min(zeile[j] + 1, zeile[j - 1] + 1,
                                             vorher + (wort_a != wort_b))
    return zeile[-1] / len(a)


def main() -> int:
    import numpy as np
    import torch
    from resemblyzer import VoiceEncoder, preprocess_wav
    from mimic import voices
    from mimic.worker import REVISIONS, request_gpu_permission

    # Stellschrauben in den Dienstcode druecken -- dieselben Konstanten,
    # die ein Gewinner-Commit spaeter dauerhaft aendern wuerde.
    voices.MIN_SATZ_ZEICHEN = MIN_SATZ_ZEICHEN
    voices.MAX_SATZ_ZEICHEN = MAX_SATZ_ZEICHEN

    referenzen = {}
    for stimme in {probe.stimme for probe in KORPUS}:
        pfad = cache_pfad(stimme)
        if not pfad.exists():
            print(f"Referenz-Cache fehlt: {pfad} -- erst prepare.py laufen lassen")
            return 2
        referenzen[stimme] = np.load(pfad)

    lauf = time.strftime("%Y%m%d-%H%M%S")
    ausgabe = FORSCHUNG / "out" / lauf
    ausgabe.mkdir(parents=True)
    start = time.monotonic()

    # Whisper VOR dem Offline-Schalter laden: HF_HUB_OFFLINE gilt fuer den
    # ganzen Prozess und wuerde den erstmaligen Modell-Download verhindern.
    # CPU mit int8, damit der Waechter nicht mit dots.tts um VRAM streitet.
    from faster_whisper import WhisperModel
    whisper = WhisperModel(WHISPER_MODELL, device="cpu", compute_type="int8")

    release = request_gpu_permission()
    try:
        from dots_tts.runtime import DotsTtsRuntime
        repo, revision = REVISIONS["mf"]
        os.environ["HF_HUB_OFFLINE"] = "1"
        with torch.device("cuda"):
            runtime = DotsTtsRuntime.from_pretrained(
                repo, revision=revision, precision="bfloat16", optimize=False)
    finally:
        release()

    encoder = VoiceEncoder(verbose=False)
    ergebnisse = []
    uebersprungen = []
    for probe in KORPUS:
        if time.monotonic() - start > BUDGET_S:
            uebersprungen.append(probe.kennung)
            continue
        profil = voices.load_voice(probe.stimme)
        if SPEAKER_SCALE is not None:
            profil = dataclasses.replace(profil, speaker_scale=SPEAKER_SCALE)
        try:
            pcm = synthese(runtime, profil, probe.text)
        finally:
            voices.close_voice(profil)
        wav_pfad = ausgabe / f"{probe.kennung}.wav"
        with wave.open(str(wav_pfad), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(runtime.sample_rate))
            wav.writeframes(pcm)
        embedding = encoder.embed_utterance(preprocess_wav(wav_pfad))
        wert = float(np.dot(referenzen[probe.stimme], embedding))
        segmente, _info = whisper.transcribe(str(wav_pfad), language=probe.sprache)
        transkript = " ".join(segment.text for segment in segmente)
        fehlerrate = round(wer(probe.text, transkript), 4)
        ergebnisse.append({"kennung": probe.kennung, "stimme": probe.stimme,
                           "aehnlichkeit": round(wert, 4), "wer": fehlerrate})
        print(f"{wert:.4f}  wer={fehlerrate:.4f}  {probe.kennung}")

    mittel = (round(sum(e["aehnlichkeit"] for e in ergebnisse) / len(ergebnisse), 4)
              if ergebnisse else 0.0)
    gueltig = bool(ergebnisse) and all(e["wer"] <= WER_DECKEL for e in ergebnisse)
    eintrag = {"lauf": lauf, "notiz": NOTIZ,
               "stellschrauben": {"min_satz": MIN_SATZ_ZEICHEN, "max_satz": MAX_SATZ_ZEICHEN,
                                  "pause_ms": PAUSE_MS, "speaker_scale": SPEAKER_SCALE},
               "mittel": mittel, "gueltig": gueltig, "wer_deckel": WER_DECKEL,
               "proben": ergebnisse, "uebersprungen": uebersprungen,
               "dauer_s": round(time.monotonic() - start, 1)}
    with JOURNAL.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    print(f"mittel={mittel} gueltig={gueltig} proben={len(ergebnisse)} "
          f"uebersprungen={len(uebersprungen)} dauer_s={eintrag['dauer_s']} -> {JOURNAL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
