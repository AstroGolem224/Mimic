"""Der Ansage-Hook, ohne Audio und ohne laufenden Dienst.

Geprueft wird das, was schiefgehen kann, ohne dass es jemand merkt: ein
Transkript, aus dem der falsche Text gezogen wird, und eine Antwort, die als
Codeblock in die Stimme laeuft. Der Sprechpfad selbst braucht Hardware und
bleibt der Handprobe in tools/ANSAGE.md ueberlassen.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import ansage  # noqa: E402

ANSAGE_PY = Path(__file__).resolve().parent.parent / "tools" / "ansage.py"


def transkript(*eintraege: dict) -> Path:
    datei = Path(tempfile.mkdtemp()) / "transcript.jsonl"
    with open(datei, "w", encoding="utf-8") as handle:
        for eintrag in eintraege:
            handle.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    return datei


def assistent(text: str, **rest) -> dict:
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            **rest}


class LetzteAntwort(unittest.TestCase):
    def test_nimmt_die_letzte_assistentennachricht(self):
        pfad = transkript(assistent("erste"), {"type": "user", "message": {"content": "frage"}},
                          assistent("zweite"))
        self.assertEqual(ansage.letzte_antwort(pfad), "zweite")

    def test_ueberspringt_subagenten(self):
        pfad = transkript(assistent("hauptstrang"), assistent("nebenlauf", isSidechain=True))
        self.assertEqual(ansage.letzte_antwort(pfad), "hauptstrang")

    def test_ueberspringt_reine_werkzeugzuege(self):
        werkzeug = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "1", "name": "Bash", "input": {}}]}}
        pfad = transkript(assistent("die Antwort"), werkzeug)
        self.assertEqual(ansage.letzte_antwort(pfad), "die Antwort")

    def test_content_als_zeichenkette(self):
        pfad = transkript({"type": "assistant", "message": {"role": "assistant", "content": "knapp"}})
        self.assertEqual(ansage.letzte_antwort(pfad), "knapp")

    def test_kaputte_zeilen_stoeren_nicht(self):
        pfad = transkript(assistent("heil"))
        with open(pfad, "a", encoding="utf-8") as handle:
            handle.write("{kein json\n")
        self.assertEqual(ansage.letzte_antwort(pfad), "heil")

    def test_fehlendes_transkript(self):
        self.assertEqual(ansage.letzte_antwort(Path("/nicht/vorhanden.jsonl")), "")

    def test_angeschnittene_erste_zeile_faellt_weg(self):
        """Nur das Dateiende wird gelesen -- die Bruchstelle darf nichts erfinden."""
        pfad = transkript(assistent("x" * (ansage.TAIL_BYTES + 100)), assistent("das Ende"))
        self.assertEqual(ansage.letzte_antwort(pfad), "das Ende")


class Zusammenfassen(unittest.TestCase):
    def test_codeblock_faellt_weg(self):
        text = "Der Test laeuft.\n```python\nprint('nicht sprechen')\n```\nAlles gruen."
        ergebnis = ansage.zusammenfassen(text)
        self.assertNotIn("print", ergebnis)
        self.assertEqual(ergebnis, "Der Test laeuft. Alles gruen.")

    def test_unbeendeter_codeblock_frisst_den_rest(self):
        self.assertEqual(ansage.zusammenfassen("Fertig.\n```\nkaputt"), "Fertig.")

    def test_auszeichnung_und_links(self):
        text = "## Ergebnis\n- **Zwei** Fehler behoben\n- Siehe [PHASE2](PHASE2.md)"
        self.assertEqual(ansage.zusammenfassen(text),
                         "Ergebnis. Zwei Fehler behoben. Siehe PHASE2.")

    def test_tabellen_und_trennlinien(self):
        text = "Stand:\n| A | B |\n|---|---|\n| 1 | 2 |\n---\nPasst."
        self.assertEqual(ansage.zusammenfassen(text), "Stand: Passt.")

    def test_pfadzeile_faellt_weg(self):
        self.assertEqual(ansage.zusammenfassen("Geaendert:\nmimic/cli.py\nLaeuft."),
                         "Geaendert: Laeuft.")

    def test_kuerzt_an_der_satzgrenze(self):
        text = " ".join(f"Satz nummer {n} steht hier." for n in range(50))
        ergebnis = ansage.zusammenfassen(text)
        self.assertLessEqual(len(ergebnis), ansage.GRENZE)
        self.assertTrue(ergebnis.endswith("."))
        self.assertTrue(ergebnis.startswith("Satz nummer 0"))

    def test_ein_einziger_langer_satz(self):
        ergebnis = ansage.zusammenfassen("wort " * 200)
        self.assertLessEqual(len(ergebnis), ansage.GRENZE + 4)
        self.assertTrue(ergebnis.endswith("..."))

    def test_leere_eingabe(self):
        self.assertEqual(ansage.zusammenfassen(""), "")
        self.assertEqual(ansage.zusammenfassen("```\nnur code\n```"), "")


class Ansagetext(unittest.TestCase):
    def test_stop_mit_transkript(self):
        pfad = transkript(assistent("Zwei Tests repariert, alles gruen."))
        text = ansage.ansagetext({"hook_event_name": "Stop", "transcript_path": str(pfad)})
        self.assertEqual(text, "Fertig. Zwei Tests repariert, alles gruen.")

    def test_stop_ohne_transkript(self):
        self.assertEqual(ansage.ansagetext({"hook_event_name": "Stop"}), ansage.OHNE_INHALT)

    def test_leere_nutzlast(self):
        self.assertEqual(ansage.ansagetext({}), ansage.OHNE_INHALT)

    def test_notification(self):
        text = ansage.ansagetext({"hook_event_name": "Notification",
                                  "message": "Claude braucht deine Erlaubnis fuer Bash"})
        self.assertEqual(text, "Claude wartet. Claude braucht deine Erlaubnis fuer Bash.")

    def test_notification_ohne_meldung(self):
        self.assertEqual(ansage.ansagetext({"hook_event_name": "Notification"}), "Claude wartet.")


class AlsHook(unittest.TestCase):
    """Der Vertrag mit Claude Code: liest stdin, endet in 0, sagt nichts weiter."""

    def lauf(self, eingabe: str, *argumente: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ANSAGE_PY), *argumente],
                              input=eingabe, capture_output=True, text=True, timeout=30,
                              env={"PATH": "/nonexistent", "HOME": tempfile.mkdtemp(),
                                   "MIMIC_ANSAGE_STILL": "1"})

    def test_vorschau(self):
        pfad = transkript(assistent("Alles erledigt."))
        lauf = self.lauf(json.dumps({"hook_event_name": "Stop", "transcript_path": str(pfad)}),
                         "--vorschau")
        self.assertEqual(lauf.returncode, 0)
        self.assertEqual(lauf.stdout.strip(), "Fertig. Alles erledigt.")

    def test_muell_auf_stdin_endet_trotzdem_in_null(self):
        lauf = self.lauf("das ist kein json")
        self.assertEqual(lauf.returncode, 0)
        self.assertEqual(lauf.stdout, "")

    def test_leeres_stdin(self):
        self.assertEqual(self.lauf("").returncode, 0)

    def test_ohne_mimic_kein_fehler(self):
        """Der Sprechpfad ohne installiertes `mimic` -- muss lautlos in 0 enden."""
        lauf = subprocess.run([sys.executable, str(ANSAGE_PY), "--sagen", "Probe"],
                              capture_output=True, text=True, timeout=30,
                              env={"PATH": "/nonexistent", "HOME": tempfile.mkdtemp(),
                                   "XDG_RUNTIME_DIR": tempfile.mkdtemp()})
        self.assertEqual(lauf.returncode, 0)
        self.assertEqual(lauf.stderr, "")


if __name__ == "__main__":
    unittest.main()
