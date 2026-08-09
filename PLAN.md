# PLAN — Lange Texte verfälschen die Klonstimme: zwei Chunking-Defekte

## Ziel

Die geklonte Stimme klingt bei langen Texten aus der GUI stark verändert. Zwei
nachgewiesene Defekte an den Chunk-Nähten beheben; danach erreicht kein
Textstück ohne Satzkontext und kein überlanges Textstück mehr das Modell.

## Diagnose (belegt, nicht ändern)

1. **`mimic/voices.py::split_sentences` kennt keine Obergrenze.** Text ohne
   Satzendezeichen (Kommas, Doppelpunkte, Listen) geht bis zur Frontend-Grenze
   von 1000 Zeichen als EIN `generate_stream`-Aufruf ans Modell. Repro: 886
   Zeichen ohne `.!?…` → 1 Chunk. Lange Einzelaufrufe verfälschen die Stimme.
2. **`mimic/gui.py::parse_skript` macht aus jeder Zeile einen eigenen
   Einsatz.** Hart umbrochener Text (kopierte Absätze) zerfällt in satzlose
   Fragmente, je Fragment ein eigener /speak mit 300 ms Sprecherpause dazwischen
   — mitten im Satz. Bei Fragmenten ohne Satzkontext halluziniert dots.tts
   (Messung Phase 0, siehe Kommentar an `MIN_SATZ_ZEICHEN`).

## Schritt 1 — Obergrenze in `split_sentences` (mimic/voices.py)

- Neue Konstante `MAX_SATZ_ZEICHEN = 250` neben `MIN_SATZ_ZEICHEN`, mit kurzem
  Kommentar im Stil der Datei (Zweck: ein Generierungsaufruf bleibt in der
  Länge, die das Modell sauber trägt; 250 ≈ zwei lange deutsche Sätze).
- Nach der bestehenden Zusammenfassungs-Logik ein Nachlauf: jeder Chunk
  länger als `MAX_SATZ_ZEICHEN` wird weiter zerlegt:
  1. Bevorzugte Schnittstelle: letztes `,`, `;`, `:`, `–` oder `—` gefolgt von
     Leerzeichen im Bereich `[MIN_SATZ_ZEICHEN, MAX_SATZ_ZEICHEN]`.
  2. Sonst: letztes Leerzeichen in diesem Bereich.
  3. Sonst (ein Wort länger als 250): harter Schnitt bei `MAX_SATZ_ZEICHEN`.
  Der Schnitt wiederholt sich, bis alle Stücke unter der Grenze liegen. Ein
  Reststück kürzer als `MIN_SATZ_ZEICHEN` wird an das vorige Stück angehängt
  (darf die Grenze dadurch leicht überschreiten — Fragmente sind das größere
  Übel als +20 Zeichen).
- Invariante: kein Wort geht verloren, Reihenfolge bleibt (bestehender
  Erhaltungs-Assert in test_10 als Vorbild).

## Schritt 2 — Absatz-Logik in `parse_skript` (mimic/gui.py)

- Aufeinanderfolgende nicht-leere Zeilen mit demselben Sprecher verschmelzen zu
  EINEM Einsatz (mit `" "` verbunden). Grenzen, die einen neuen Einsatz
  beginnen: Leerzeile, Sprecherwechsel (`#name:`-Präfix). Kommentarzeilen
  (`//`) werden weiterhin übersprungen und beenden den Absatz NICHT.
- Anführungszeichen-Strippen bleibt je Zeile (vor dem Verbinden), wie bisher.
- Docstring von gui.py (Format-Beschreibung, Zeilen 1–17) an das neue Verhalten
  anpassen: „Aufeinanderfolgende Zeilen desselben Sprechers bilden einen
  Absatz; eine Leerzeile trennt Einsätze."
- `gui.demo()` anpassen: `parse_skript('#a: "eins"\nzwei\n// weg\n', "z")`
  ergibt jetzt `[Einsatz("a", "eins zwei")]`.

## Schritt 3 — Tests (tests/test_phase1.py, Klasse TextAndLevelTests)

- Neuer Test: `split_sentences` mit >250 Zeichen ohne Satzendezeichen liefert
  nur Chunks `<= MAX_SATZ_ZEICHEN + MIN_SATZ_ZEICHEN`, jeder
  `>= MIN_SATZ_ZEICHEN` (bei mehr als einem Chunk), Wörter bleiben erhalten.
- Neuer Test: `split_sentences` mit normalem Mehrsatztext bleibt unverändert
  (Regressionsschutz: Sätze unter der Grenze werden NICHT zerschnitten).
- Neuer Test: `parse_skript` mit hart umbrochenem Absatz (drei Zeilen, ein
  Satzgefüge, ein Sprecher) ergibt genau EINEN Einsatz mit verbundenem Text.
- Bestehender `test_15_skript_zerlegung` bleibt GRÜN und UNVERÄNDERT: dort
  trennt eine Leerzeile die beiden Krieger-Zeilen — sie bleiben zwei Einsätze.

## Out of scope (nicht anfassen)

- `mimic/worker.py` (Pausenlogik, Stumm-Erkennung, Retry) bleibt unverändert.
- `mimic/frontend.py`, Grenzen (`MAX_TEXT_CHARS` 1000) bleiben unverändert.
- Keine neuen Abhängigkeiten, keine Umbenennungen, kein Refactoring daneben.
- ASCII-Umlaute-Konvention der Kommentare (ae/oe/ue) beibehalten.

## Proof

`tests/run.sh` läuft grün (CPU-Variante, ohne `--gpu`).
