"""Browser-GUI fuer Qwen3-TTS: Stimme klonen, sprechen lassen, als MP3 speichern.

Start:  uv run python app.py       ->  http://127.0.0.1:7860

Stimmen liegen als Verzeichnis unter ~/.local/share/qwentts/voices/<name>/
mit ref.wav und ref.txt -- dasselbe Schema wie Mimic, damit man Referenzen
zwischen beiden hin und her kopieren kann.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import gradio as gr
import soundfile as sf

MODELL = os.environ.get("QWENTTS_MODELL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
STIMMEN = Path(os.environ.get("QWENTTS_VOICES", Path.home() / ".local/share/qwentts/voices"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
SPRACHEN = ["German", "English", "Chinese", "Japanese", "Korean", "French",
            "Russian", "Portuguese", "Spanish", "Italian", "Auto"]

_modell = None


def modell():
    """Erst beim ersten Sprechen laden -- der Start soll nicht auf 4.5 GB warten."""
    global _modell
    if _modell is None:
        import torch
        from qwen_tts import Qwen3TTSModel
        # Kein flash_attention_2: das will kompiliert werden und kostet mehr Zeit,
        # als es hier spart. sdpa ist der Transformers-Standard und reicht.
        _modell = Qwen3TTSModel.from_pretrained(MODELL, device_map="cuda:0",
                                                dtype=torch.bfloat16)
    return _modell


def stimmen() -> list[str]:
    if not STIMMEN.is_dir():
        return []
    return sorted(v.name for v in STIMMEN.iterdir()
                  if (v / "ref.wav").is_file() and (v / "ref.txt").is_file())


def klonen(aufnahme: str | None, transkript: str, name: str):
    """Legt ein Stimmprofil an. Prueft, was der Klon spaeter kaputtmachen wuerde."""
    name = (name or "").strip()
    transkript = " ".join((transkript or "").split())
    if not aufnahme:
        return "Keine Aufnahme.", gr.update()
    if not NAME_RE.fullmatch(name):
        return "Name: Buchstaben, Ziffern, _ und -, hoechstens 32 Zeichen.", gr.update()
    if not transkript:
        return "Transkript fehlt -- ohne den gesprochenen Wortlaut wird der Klon schlechter.", gr.update()
    daten, rate = sf.read(aufnahme, always_2d=True)
    dauer = len(daten) / rate
    if dauer < 3:
        return f"{dauer:.1f} s ist zu kurz, Qwen3-TTS will mindestens 3 s.", gr.update()
    if dauer > 30:
        return f"{dauer:.1f} s ist zu lang; 3 bis 10 s sind laut Modellkarte der Punkt.", gr.update()
    ziel = STIMMEN / name
    ziel.mkdir(mode=0o700, parents=True, exist_ok=True)
    sf.write(ziel / "ref.wav", daten[:, :1], rate)      # mono, Rate bleibt wie aufgenommen
    (ziel / "ref.txt").write_text(transkript + "\n", encoding="utf-8")
    liste = stimmen()
    return (f"'{name}' gespeichert ({dauer:.1f} s).",
            gr.update(choices=liste, value=name))


def als_mp3(wav: Path) -> str:
    ziel = wav.with_suffix(".mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                    "-codec:a", "libmp3lame", "-q:a", "2", str(ziel)], check=True)
    return str(ziel)


def sprechen(stimme: str, text: str, sprache: str):
    text = (text or "").strip()
    if not stimme:
        return None, None, "Keine Stimme gewaehlt."
    if not text:
        return None, None, "Kein Text."
    profil = STIMMEN / stimme
    wavs, rate = modell().generate_voice_clone(
        text=text,
        language=sprache,
        ref_audio=str(profil / "ref.wav"),
        ref_text=(profil / "ref.txt").read_text(encoding="utf-8").strip(),
    )
    ordner = Path(tempfile.mkdtemp(prefix="qwentts-"))
    wav = ordner / f"{stimme}.wav"
    sf.write(wav, wavs[0], rate)
    # MP3 gleich mit erzeugen statt auf einen zweiten Knopf zu warten: die
    # Umwandlung kostet Millisekunden, eine zweite Synthese kostet Sekunden.
    mp3 = als_mp3(wav)
    dauer = len(wavs[0]) / rate
    return str(wav), mp3, f"{dauer:.1f} s erzeugt."


with gr.Blocks(title="Qwen3-TTS") as ui:
    gr.Markdown("# Qwen3-TTS\nStimme klonen, sprechen lassen, als MP3 laden.")

    with gr.Tab("Sprechen"):
        with gr.Row():
            wahl = gr.Dropdown(choices=stimmen(), label="Stimme", scale=2,
                               value=(stimmen() or [None])[0])
            sprache = gr.Dropdown(choices=SPRACHEN, value="German", label="Sprache", scale=1)
            neu_lesen = gr.Button("Stimmen neu lesen", scale=1)
        eingabe = gr.Textbox(label="Text", lines=6,
                             placeholder="Der Turm steht offen, und niemand bewacht ihn.")
        los = gr.Button("Sprechen", variant="primary")
        ausgabe = gr.Audio(label="Ergebnis", autoplay=True)
        datei = gr.File(label="MP3")
        meldung = gr.Markdown()

    with gr.Tab("Stimme klonen"):
        gr.Markdown("3 bis 10 Sekunden aufnehmen oder hochladen, Wortlaut daneben "
                    "eintragen. Das Transkript muss genau das sein, was zu hoeren ist.")
        aufnahme = gr.Audio(sources=["microphone", "upload"], type="filepath",
                            label="Referenz")
        transkript = gr.Textbox(label="Wortlaut der Aufnahme", lines=3)
        name = gr.Textbox(label="Name der Stimme", placeholder="matthias")
        speichern = gr.Button("Stimme speichern", variant="primary")
        klon_meldung = gr.Markdown()

    los.click(sprechen, [wahl, eingabe, sprache], [ausgabe, datei, meldung])
    neu_lesen.click(lambda: gr.update(choices=stimmen()), None, wahl)
    speichern.click(klonen, [aufnahme, transkript, name], [klon_meldung, wahl])


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg fehlt -- ohne das gibt es kein MP3.")
    ui.launch(inbrowser=True)
