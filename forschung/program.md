# program.md — Anweisungen für den Nachtagenten

Du experimentierst an den Chunking-Stellschrauben des Mimic-TTS. Ziel: die
mittlere Sprecher-Ähnlichkeit (`mittel` im Journal) erhöhen, gemessen am
festen Korpus. Vorbild: karpathy/autoresearch — ändern, messen, behalten
oder verwerfen.

## Setup (einmal)

1. `forschung/lauf.sh` ausführen. Der erste Lauf mit `NOTIZ = "baseline"`
   ist die Vergleichsbasis; sein `mittel` steht in `forschung/journal.jsonl`.

## Schleife (je Experiment)

1. **Eine** Änderung im Stellschrauben-Block von `forschung/experiment.py`
   (zwischen den Markierungen). `NOTIZ` auf eine Zeile setzen, die sagt, was
   geprüft wird — z. B. "max_satz 180 statt 250".
2. `forschung/lauf.sh` ausführen.
3. `mittel` des neuen Laufs gegen das beste bisherige `mittel` im Journal
   vergleichen.
   - Besser → Stellschraube behalten, weiter mit Schritt 1.
   - Schlechter oder gleich → Stellschraube auf den letzten besten Stand
     zurücksetzen, andere Änderung probieren.
4. Nach jedem Lauf zwei Sätze ins Journal-Feld deiner Antwort: was geändert,
   was gemessen.

## Regeln

- Editiert wird NUR der Stellschrauben-Block in `experiment.py`. Nicht
  `prepare.py`, nicht `program.md`, nicht der Messapparat unter dem Block,
  nichts unter `mimic/`.
- Eine Änderung je Lauf. Zwei gleichzeitig = Messung wertlos.
- Bricht ein Lauf mit `load_denied` (GPU-Hub: fullscreen/VRAM) oder CUDA-OOM
  ab: das ist KEIN Messwert. Warten, später erneut — nicht als
  Verschlechterung zählen. Hält ein anderer Prozess (z. B. ollama) den VRAM,
  darfst du `ollama stop <modell>` ausführen, sonst nichts beenden.
- Stellschrauben-Grenzen: `MIN_SATZ_ZEICHEN` 10–60, `MAX_SATZ_ZEICHEN`
  80–500, `PAUSE_MS` 0–600, `SPEAKER_SCALE` None oder 0.5–2.0 (Grenzen des
  Dienstes, siehe mimic/voices.py), `MODUS` "mf" oder "soar". `mittel`-Werte
  sind nur zwischen Läufen mit demselben `MODUS` vergleichbar.
- Stopp nach 12 Läufen oder wenn drei Änderungen in Folge nichts verbessert
  haben. Dann: bestes Journal-Ergebnis nennen, Stellschrauben auf den
  Gewinnerstand setzen, NICHT committen — der Gewinner wird am Morgen von
  einem Menschen reviewt und regulär übernommen.

## WER-Wächter und Streuung

Jeder Lauf trägt `gueltig` im Journal: eine Probe mit Wortfehlerrate über
`WER_DECKEL` (0.25) macht den Lauf ungültig — dots.tts verschluckt bei langen
Kommatexten stochastisch ganze Chunks, die Ähnlichkeit bleibt dabei
unauffällig. Daraus folgt:

- Nur gültige Läufe zählen im Vergleich der `mittel`-Werte.
- Die Baseline-Läufe vom 2026-08-10 waren BEIDE ungültig (je eine
  verschluckte Probe). Erstes Ziel der Schleife ist deshalb nicht mehr
  Ähnlichkeit, sondern: eine Stellschraubenlage finden, die zuverlässig
  gültige Läufe liefert — z. B. kleineres `MAX_SATZ_ZEICHEN`.
- Messwerte streuen. Eine vielversprechende Änderung mit einem zweiten Lauf
  bei gleicher Einstellung bestätigen (NOTIZ: "... bestaetigung"), bevor sie
  als besser gilt.
