# PLAN-REVIEW-LOG — Lange Texte verfälschen die Klonstimme

## Act 3 — Build

### Round 1 — Codex build (Thread 019fe7ce-8318-7921-9e0a-ed8117d2c0ee, gpt-5.6-sol)

- mimic/voices.py: `MAX_SATZ_ZEICHEN = 250` und Nachlauf in `split_sentences`,
  der überlange Chunks an Klausel-Interpunktion, sonst am Leerzeichen, sonst
  hart schneidet; Reststücke < MIN hängen am Vorgänger.
- mimic/gui.py: `parse_skript` verschmilzt aufeinanderfolgende Zeilen desselben
  Sprechers zu einem Einsatz; Leerzeile und Sprecherwechsel trennen,
  `//`-Zeilen unterbrechen den Absatz nicht. Docstring und `demo()` angepasst.
- tests/test_phase1.py: test_10a (Längendeckel + Worterhalt), test_10b
  (normale Sätze unverändert), test_10c (hart umbrochener Absatz = ein
  Einsatz). test_15 unverändert grün.
- Codex-Bericht: 92 Tests OK, keine Abweichungen vom Spec.

### Claude's verdict

- Diff vollständig gelesen: spec-treu, Stil passt, nichts außerhalb des
  Umfangs (worker.py, frontend.py unberührt).
- Beweis selbst geführt: `tests/run.sh` → 92 Tests OK;
  `MIMIC_GUI_DEMO=1 python -m mimic.gui` → demo ok.
- Schnittlogik geprüft: Terminierung gesichert (Schnitt ≥ MIN_SATZ_ZEICHEN),
  kein Wortverlust, Interpunktion bleibt beim vorderen Stück.
- Runden verbraucht: 1 von 2. Keine Nacharbeit nötig.
