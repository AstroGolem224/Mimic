# qwen-tts-gui — geparkt

Browser-GUI für [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) (Apache-2.0):
Stimme klonen, Text sprechen lassen, als MP3 laden. Gebaut als Alternative zu
**Mimic** (dots.tts) — dieselbe Aufgabe, anderes Modell, zum Vergleichen.

**Ergebnis des Vergleichs am 2026-08-05: dots.tts bleibt.** Matthias hat beide
mit derselben Referenzstimme gehört und dots.tts als besser beurteilt. Das ist
ein Hörurteil, keine Messung — die technischen Zahlen unten sprachen nicht
dagegen, entschieden hat das Ohr. Der Code bleibt lauffähig liegen, falls
Qwen3-TTS später nachlegt oder eine zweite Meinung gebraucht wird.

```bash
cd alternative/qwen3 && uv run python app.py      # öffnet http://127.0.0.1:7860
```

Erster Start lädt `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (4.5 GB) nach
`~/.cache/huggingface`. Das Modell kommt erst beim ersten *Sprechen* in den
Speicher, nicht beim Start.

## Bedienung

**Sprechen** — Stimme wählen, Sprache wählen (Vorgabe Deutsch), Text eintippen,
*Sprechen*. Das Ergebnis spielt automatisch ab; darunter liegt dieselbe Ausgabe
als MP3 zum Herunterladen. Kein zweiter Knopf dafür: die Umwandlung kostet
Millisekunden, eine zweite Synthese kostet Sekunden.

**Stimme klonen** — 3 bis 10 Sekunden aufnehmen oder hochladen, den Wortlaut
genau so eintragen wie gesprochen, Namen vergeben, speichern. Ohne Transkript
geht es zwar auch (`x_vector_only_mode`), laut Modellkarte aber schlechter —
deshalb verlangt das GUI es.

## Stimmen

Liegen als `~/.local/share/qwentts/voices/<name>/` mit `ref.wav` und `ref.txt`
— dasselbe Schema wie Mimic, Referenzen sind zwischen beiden kopierbar. Die
Mimic-Profile sind beim Aufsetzen einmal herüberkopiert worden.

## Unterschiede zu Mimic

| | Mimic (dots.tts) | hier (Qwen3-TTS) |
|---|---|---|
| Bedienung | tkinter-Fenster, systemd-Dienst, Unix-Socket | ein Gradio-Prozess im Browser |
| Referenz | 10–15 s, 48 kHz mono, feste Profile | 3–10 s, Rate egal |
| Sprachen | Sprach-Tag `en` auch für Deutsch | 10 Sprachen, Deutsch nativ |
| Ausgabe | WAV, Streaming ab ~250 ms | WAV + MP3, komplett am Stück |

Bewusst weggelassen: kein Streaming, kein Dienst, keine Warteschlange, kein
Stopp-Knopf. Das war die Bedingung, unter der das Ding in einer Stunde stand.
Hätte Qwen gewonnen, wäre es hinter Mimics Worker gewandert statt dieses GUI
auszubauen — es hat nicht gewonnen, also bleibt es, wie es ist.

Gemessen am 2026-08-05, RTX 5090: Modell lädt in 7.3 s, Synthese warm mit
RTF 0.95, Ausgabe 24 kHz mono. Mimic liegt bei RTF ~0.73 und 48 kHz, streamt
aber zusätzlich ab ~250 ms, während hier erst am Stück gerechnet wird.
