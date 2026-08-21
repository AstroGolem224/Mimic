"""Der Ansage-Hook, ohne Audio und ohne laufenden Dienst.

Geprueft wird das, was schiefgehen kann, ohne dass es jemand merkt: ein
Transkript, aus dem der falsche Text gezogen wird, und eine Antwort, die als
Codeblock in die Stimme laeuft. Der Sprechpfad selbst braucht Hardware und
bleibt der Handprobe in tools/ANSAGE.md ueberlassen.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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


class Sprechbar(unittest.TestCase):
    """Pfade werden gesprochen wie ein Mensch sie vorliest, nicht gestrichen."""

    def test_pfad_wird_buchstabiert(self):
        # Nur der fuehrende Schraegstrich wird gesprochen, innere sind Pausen.
        self.assertEqual(ansage.sprechbar("/run/user/1000/mimic-ansage.stimme"),
                         "slash run user 1000 mimic ansage punkt stimme")

    def test_relativer_pfad(self):
        self.assertEqual(ansage.sprechbar("tools/ansage.py"), "tools ansage punkt py")

    def test_tilde_und_versteckter_ordner(self):
        self.assertEqual(ansage.sprechbar("~/.local/bin"), "tilde punkt local bin")

    def test_zeilennummer_wird_ausgesprochen(self):
        self.assertEqual(ansage.sprechbar("mimic/cli.py:195"), "mimic cli punkt py Zeile 195")

    def test_uuid_wird_zur_kennung(self):
        # 36 Zeichen vorzulesen stiftet keinen Nutzen.
        self.assertEqual(ansage.sprechbar("stimme.080205dd-44d5-4c94-9ad5-977813115da7"),
                         "stimme punkt eine Kennung")

    def test_commit_hash_wird_zur_kennung(self):
        self.assertEqual(ansage.sprechbar("5341a99"), "eine Kennung")

    def test_unterstrich_wird_zur_luecke(self):
        self.assertEqual(ansage.sprechbar("test_ansage.py"), "test ansage punkt py")


class Blockbeschreibung(unittest.TestCase):
    """Codebloecke werden gedeutet, nicht vorgelesen."""

    def test_shell_nennt_die_befehle(self):
        block = "```bash\necho geth > datei\n```"
        self.assertEqual(ansage.blockbeschreibung(block),
                         "Ein Bash-Block mit 1 Zeile, ruft echo auf.")

    def test_shell_mit_mehreren_befehlen(self):
        block = "```bash\ngit add datei\ngit commit\npython3 pruefen.py\n```"
        self.assertEqual(ansage.blockbeschreibung(block),
                         "Ein Bash-Block mit 3 Zeilen, ruft git und python3 auf.")

    def test_python_nennt_die_definitionen(self):
        block = "```python\ndef stimme():\n    return 1\n\ndef stimmdatei():\n    return 2\n```"
        self.assertEqual(ansage.blockbeschreibung(block),
                         "Ein Python-Block mit 4 Zeilen, definiert stimme und stimmdatei.")

    def test_ohne_sprache_nur_der_umfang(self):
        self.assertEqual(ansage.blockbeschreibung("```\na\nb\n```"), "Ein Codeblock mit 2 Zeilen.")

    def test_leerer_block(self):
        self.assertEqual(ansage.blockbeschreibung("```bash\n```"), "Ein leerer Bash-Block.")


class Zusammenfassen(unittest.TestCase):
    def test_codeblock_wird_beschrieben(self):
        text = "Der Test laeuft.\n```python\nprint('nicht sprechen')\n```\nAlles gruen."
        ergebnis = ansage.zusammenfassen(text)
        self.assertNotIn("print", ergebnis)
        self.assertEqual(ergebnis, "Der Test laeuft. Ein Python-Block mit 1 Zeile. Alles gruen.")

    def test_unbeendeter_codeblock_frisst_den_rest(self):
        self.assertEqual(ansage.zusammenfassen("Fertig.\n```\nkaputt"),
                         "Fertig. Ein Codeblock mit 1 Zeile.")

    def test_auszeichnung_und_links(self):
        text = "## Ergebnis\n- **Zwei** Fehler behoben\n- Der Rest passt"
        self.assertEqual(ansage.zusammenfassen(text),
                         "Ergebnis. Zwei Fehler behoben. Der Rest passt.")

    def test_bezeichner_in_backticks_wird_gesprochen(self):
        """Ohne ihn fehlt dem Satz sein Subjekt, nicht nur ein Detail."""
        text = "Die Vorgabe steht in `VORGABE_STIMME` und greift sofort."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Die Vorgabe steht in VORGABE STIMME und greift sofort.")

    def test_mehrteiliger_befehl_wird_angesagt_statt_vorgelesen(self):
        text = "Danach `uv tool install --python 3.12 .` laufen lassen."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Danach ein Befehl laufen lassen.")

    def test_bezeichner_zeichen_werden_zu_worten(self):
        faelle = {"`_fenster()`": "fenster", "`webbrowser.open`": "webbrowser punkt open",
                  "`SameSite=Strict`": "SameSite gleich Strict",
                  "`127.0.0.1:1234`": "127.0.0.1 Port 1234", "`127.0.0.1`": "127.0.0.1"}
        for quelle, erwartet in faelle.items():
            with self.subTest(quelle=quelle):
                self.assertEqual(ansage.zusammenfassen(f"Der Fall {quelle} bleibt offen."),
                                 f"Der Fall {erwartet} bleibt offen.")

    def test_dateiname_und_verzeichnis_im_fliesstext_werden_gesprochen(self):
        text = "Der Filter sitzt in tools/ansage.py und deckt auch ~/.local/bin ab."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Der Filter sitzt in tools ansage punkt py und deckt auch "
                         "tilde punkt local bin ab.")

    def test_dateilink_spricht_seinen_linktext(self):
        text = "Geaendert in [cli.py](mimic/cli.py:195), Tests bleiben gruen."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Geaendert in cli punkt py, Tests bleiben gruen.")

    def test_pfad_in_backticks_wird_gesprochen(self):
        text = "Fertig in `mimic/worker.py`, alles gruen."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Fertig in mimic worker punkt py, alles gruen.")

    def test_zeile_nur_aus_bezeichnern_faellt_weg(self):
        text = "Fertig. `mimic say --voice forge`\nDer Rest bleibt."
        self.assertEqual(ansage.zusammenfassen(text), "Fertig. Der Rest bleibt.")

    def test_tabelle_wird_beschrieben(self):
        text = "Stand:\n| A | B |\n|---|---|\n| 1 | 2 |\n---\nPasst."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Stand: Eine Tabelle mit 1 Zeile. Passt.")

    def test_pfadzeile_wird_gesprochen(self):
        self.assertEqual(ansage.zusammenfassen("Geaendert:\nmimic/cli.py\nLaeuft."),
                         "Geaendert: mimic cli punkt py. Laeuft.")

    def test_ankuendigung_traegt_jetzt_ihren_block(self):
        """"Am PC:" kuendigt den Block an -- der wird beschrieben, nicht gestrichen."""
        text = "Erledigt. Am PC:\n```bash\ngit pull\n```\nDanach laeuft es."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Erledigt. Am PC: Ein Bash-Block mit 1 Zeile, ruft git auf. "
                         "Danach laeuft es.")

    def test_doppelpunkt_trennt_keinen_satz(self):
        """Einleitung und Aussage gehoeren zusammen, sonst endet die Ansage am Anlauf."""
        text = "Der Grund ist simpel: der Dienst lief nicht."
        self.assertEqual(ansage.zusammenfassen(text), text)

    def test_satzpunkt_hinter_dem_pfad_bleibt_satzzeichen(self):
        """Sonst endet der Satz auf "punkt punkt" -- einmal gesprochen, einmal gesetzt."""
        self.assertEqual(ansage.zusammenfassen("Die Datei liegt unter /etc/hosts."),
                         "Die Datei liegt unter slash etc hosts.")

    def test_freistehende_kennung_wird_nicht_buchstabiert(self):
        """Ein Hash im Fliesstext ist derselbe Fall wie einer im Pfad."""
        text = "Commit 5341a99 steht, Zweig main ist sauber."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Commit eine Kennung steht, Zweig main ist sauber.")

    def test_zahl_und_wort_bleiben_unangetastet(self):
        text = "Die Grenze steht bei 420 Zeichen, 3 Tests decken sie ab."
        self.assertEqual(ansage.zusammenfassen(text), text)

    def test_url_wird_zur_domain(self):
        """Der ganze Pfad einer Adresse taugt nicht zum Vorlesen, die Domain schon."""
        text = "Skill steht: https://github.com/AstroGolem224/Mimic/pull/2 ist offen."
        self.assertEqual(ansage.zusammenfassen(text),
                         "Skill steht: ein Link auf github punkt com ist offen.")

    def test_ungekuerzt_kommt_alles_durch(self):
        """grenze=0 schaltet die Kuerzung ab: gesprochen wird der ganze Fliesstext."""
        lang = "Der Kern der Sache ist ziemlich verwickelt " + "und geht noch weiter " * 30
        ergebnis = ansage.zusammenfassen(f"Gemerged. {lang}.", grenze=0)
        self.assertTrue(ergebnis.startswith("Gemerged. Der Kern der Sache"), ergebnis)
        self.assertTrue(ergebnis.endswith("weiter."), ergebnis)

    def test_langer_zweiter_satz_wird_angeschnitten_statt_verschluckt(self):
        """Mit gesetzter Grenze: kurzer Auftakt, lange Aussage, beides muss ankommen."""
        lang = "Der Kern der Sache ist ziemlich verwickelt " + "und geht noch weiter " * 30
        ergebnis = ansage.zusammenfassen(f"Gemerged. {lang}.", grenze=650)
        self.assertTrue(ergebnis.startswith("Gemerged. Der Kern der Sache"), ergebnis)
        self.assertTrue(ergebnis.endswith("..."), ergebnis)
        self.assertLessEqual(len(ergebnis), 654)

    def test_kurzer_auftakt_plus_passender_satz_bleibt_ganz(self):
        text = "Gemerged. Lokales main nachgezogen, Arbeitsverzeichnis sauber."
        self.assertEqual(ansage.zusammenfassen(text), text)

    def test_kuerzt_an_der_satzgrenze(self):
        text = " ".join(f"Satz nummer {n} steht hier." for n in range(50))
        ergebnis = ansage.zusammenfassen(text, grenze=650)
        self.assertLessEqual(len(ergebnis), 650)
        self.assertTrue(ergebnis.endswith("."))
        self.assertTrue(ergebnis.startswith("Satz nummer 0"))

    def test_ein_einziger_langer_satz(self):
        ergebnis = ansage.zusammenfassen("wort " * 200, grenze=650)
        self.assertLessEqual(len(ergebnis), 654)
        self.assertTrue(ergebnis.endswith("..."))


class Stueckeln(unittest.TestCase):
    """Der Dienst nimmt 1000 Zeichen je Anfrage -- laengeres wird zerlegt."""

    def test_kurzer_text_bleibt_ein_stueck(self):
        self.assertEqual(["Kurz und gut."], ansage._stuecke("Kurz und gut."))

    def test_leerer_text_ergibt_nichts(self):
        self.assertEqual([], ansage._stuecke("   "))

    def test_schnitt_liegt_auf_satzgrenzen(self):
        text = " ".join(f"Das ist Satz nummer {n}." for n in range(60))
        stuecke = ansage._stuecke(text, grenze=200)
        self.assertGreater(len(stuecke), 3)
        for stueck in stuecke:
            self.assertLessEqual(len(stueck), 200)
            self.assertTrue(stueck.endswith("."), stueck)
        self.assertEqual(text, " ".join(stuecke))      # nichts verloren, nichts doppelt

    def test_einzelner_uebergrosser_satz_bleibt_ganz(self):
        # Lieber eine Absage vom Dienst als ein Satz, der mittendrin abbricht.
        satz = "wort " * 100 + "ende."
        self.assertEqual([satz.strip()], ansage._stuecke(satz, grenze=200))

    def test_leere_eingabe(self):
        self.assertEqual(ansage.zusammenfassen(""), "")
        # Eine Antwort, die nur aus Code besteht, meldet ihren Umfang --
        # Stille waere hier nicht weniger falsch, nur leiser.
        self.assertEqual(ansage.zusammenfassen("```\nnur code\n```"),
                         "Ein Codeblock mit 1 Zeile.")


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


class Stimme(unittest.TestCase):
    def leeres_laufzeitverzeichnis(self) -> str:
        return str(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def test_vorgabe(self):
        with unittest.mock.patch.dict(
                os.environ, {"XDG_RUNTIME_DIR": self.leeres_laufzeitverzeichnis()}, clear=True):
            self.assertEqual(ansage.stimme(), "forge")

    def test_umgebung_sticht(self):
        with unittest.mock.patch.dict(os.environ, {"MIMIC_ANSAGE_STIMME": "matthias_magier"}):
            self.assertEqual(ansage.stimme(), "matthias_magier")

    def test_leere_umgebung_faellt_auf_die_vorgabe_zurueck(self):
        with unittest.mock.patch.dict(
                os.environ, {"MIMIC_ANSAGE_STIMME": "  ",
                             "XDG_RUNTIME_DIR": self.leeres_laufzeitverzeichnis()}):
            self.assertEqual(ansage.stimme(), "forge")

    def test_persona_datei_sticht_die_vorgabe(self):
        # Die Persona-Skills schreiben diese Datei beim Umschalten. Fehlt der
        # Mechanismus, spricht jede Persona mit der Vorgabestimme weiter.
        laufzeit = self.leeres_laufzeitverzeichnis()
        (Path(laufzeit) / "mimic-ansage.stimme").write_text("glados\n", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": laufzeit}, clear=True):
            self.assertEqual(ansage.stimme(), "glados")

    def test_sitzungsdatei_gilt_nur_fuer_ihre_sitzung(self):
        # Der Bug: eine Persona in Sitzung A stellte auch Sitzung B um.
        laufzeit = self.leeres_laufzeitverzeichnis()
        (Path(laufzeit) / "mimic-ansage.stimme.aaa").write_text("glados\n", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": laufzeit}, clear=True):
            self.assertEqual(ansage.stimme("aaa"), "glados")
            self.assertEqual(ansage.stimme("bbb"), "forge")

    def test_sitzungsdatei_sticht_die_gemeinsame(self):
        laufzeit = self.leeres_laufzeitverzeichnis()
        (Path(laufzeit) / "mimic-ansage.stimme").write_text("geth\n", encoding="utf-8")
        (Path(laufzeit) / "mimic-ansage.stimme.aaa").write_text("glados\n", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": laufzeit}, clear=True):
            self.assertEqual(ansage.stimme("aaa"), "glados")
            self.assertEqual(ansage.stimme("bbb"), "geth")


class Sitzungstitel(unittest.TestCase):
    """Mehrere Sitzungen teilen sich die Ausgabe -- der Titel ordnet sie zu."""

    @staticmethod
    def titel(text: str) -> dict:
        return {"type": "custom-title", "customTitle": text, "sessionId": "abc"}

    def test_titel_wird_gefunden(self):
        pfad = transkript(self.titel("Alter Titel"), assistent("egal"), self.titel("Neuer Titel"))
        self.assertEqual("Neuer Titel", ansage.sitzungstitel(pfad))

    def test_ohne_titel_leer(self):
        self.assertEqual("", ansage.sitzungstitel(transkript(assistent("nur Text"))))

    def test_titel_steht_vor_der_meldung(self):
        pfad = transkript(self.titel("Stimmen bauen"), assistent("Zwei Fehler behoben."))
        self.assertEqual("Stimmen bauen. Fertig. Zwei Fehler behoben.",
                         ansage.ansagetext({"hook_event_name": "Stop",
                                            "transcript_path": str(pfad)}))

    def test_ohne_titel_bleibt_die_meldung_unveraendert(self):
        pfad = transkript(assistent("Zwei Fehler behoben."))
        self.assertEqual("Fertig. Zwei Fehler behoben.",
                         ansage.ansagetext({"hook_event_name": "Stop",
                                            "transcript_path": str(pfad)}))

    def test_titel_auch_bei_der_nachfrage(self):
        pfad = transkript(self.titel("Stimmen bauen"))
        self.assertEqual("Stimmen bauen. Claude wartet.",
                         ansage.ansagetext({"hook_event_name": "Notification",
                                            "transcript_path": str(pfad)}))

    def test_langer_titel_wird_gekappt(self):
        pfad = transkript(self.titel("W" * 200))
        self.assertEqual(ansage.TITEL_GRENZE, len(ansage.sitzungstitel(pfad)))


class FrischeAntwort(unittest.TestCase):
    """Der Stop-Hook feuert, bevor die Antwort im Transkript steht."""

    def test_kennung_kommt_mit(self):
        pfad = transkript(assistent("erste", uuid="a"), assistent("zweite", uuid="b"))
        self.assertEqual(("zweite", "b"), ansage.letzte_antwort_mit_kennung(pfad))

    def test_wartet_bis_die_neue_antwort_erscheint(self):
        pfad = transkript(assistent("die alte Antwort ist hier.", uuid="alt"))
        laufzeit = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (laufzeit / f"mimic-ansage.{pfad.stem}.zuletzt").write_text("alt\n", encoding="utf-8")
        gesprochen = []

        def nachschieben(_dauer):
            with open(pfad, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(assistent("die neue Antwort ist da.", uuid="neu")) + "\n")

        with (unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(laufzeit)}),
              unittest.mock.patch.object(ansage.time, "sleep", side_effect=nachschieben),
              unittest.mock.patch.object(ansage, "sprechen",
                                         side_effect=lambda satz, *_a, **_k:
                                         gesprochen.append(satz) or 0)):
            self.assertEqual(0, ansage.melden(str(pfad)))
        self.assertEqual(["Fertig. die neue Antwort ist da."], gesprochen)
        # Die Kennung ist fortgeschrieben, sonst spraeche die naechste Ansage dasselbe.
        self.assertEqual("neu", (laufzeit / f"mimic-ansage.{pfad.stem}.zuletzt")
                         .read_text(encoding="utf-8").strip())

    def test_ohne_neue_antwort_wird_geschwiegen(self):
        pfad = transkript(assistent("unveraendert seit vorhin.", uuid="alt"))
        laufzeit = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (laufzeit / f"mimic-ansage.{pfad.stem}.zuletzt").write_text("alt\n", encoding="utf-8")
        gesprochen = []
        # Die Uhr springt sofort ueber die Frist: kein echtes Warten im Test.
        zeiten = iter([0.0, ansage.WARTE_FRIST_S + 1])
        with (unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(laufzeit)}),
              unittest.mock.patch.object(ansage.time, "monotonic", side_effect=lambda: next(zeiten)),
              unittest.mock.patch.object(ansage, "sprechen",
                                         side_effect=lambda satz, *_a, **_k:
                                         gesprochen.append(satz) or 0)):
            self.assertEqual(0, ansage.melden(str(pfad)))
        self.assertEqual([], gesprochen)


class Vorspann(unittest.TestCase):
    def test_titel_wird_nicht_gedoppelt(self):
        # Sobald eine Antwort die Ansage zitiert, steht der Titel schon im Text.
        pfad = transkript({"type": "custom-title", "customTitle": "Stimmen bauen",
                           "sessionId": "abc"},
                          assistent("Stimmen bauen. Fertig. Alles gruen."))
        self.assertEqual("Stimmen bauen. Fertig. Alles gruen.",
                         ansage.ansagetext({"hook_event_name": "Stop",
                                            "transcript_path": str(pfad)}))

    def test_rest_vom_gestrichenen_bezeichner_faellt_weg(self):
        self.assertEqual("Drin. Ab jetzt gilt das hier.",
                         ansage.zusammenfassen("Drin, `30a5628`. Ab jetzt gilt das hier."))


class Warteschlange(unittest.TestCase):
    """Eigene Sitzung verdraengen, fremde abwarten -- sonst reden Fenster ueber Kreuz."""

    def setUp(self):
        self.laufzeit = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(unittest.mock.patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": str(self.laufzeit)}))

    def besitzer(self, pid: int, sitzung: str) -> None:
        (self.laufzeit / "mimic-ansage.pid").write_text(f"{pid} {sitzung}\n", encoding="utf-8")

    def test_freie_ausgabe_wird_sofort_belegt(self):
        griff = ansage._sperre_holen("eigene")
        self.assertIsNotNone(griff)
        self.addCleanup(griff.close)

    def test_eigene_sitzung_wird_verdraengt(self):
        halter = ansage._sperre()                      # belegt die Ausgabe
        self.assertIsNotNone(halter)
        self.besitzer(4242, "eigene")
        verdraengt = []

        def freigeben():
            verdraengt.append(True)
            halter.close()                             # der verdraengte gibt die Sperre frei

        with (unittest.mock.patch.object(ansage, "_verdraenge_laufende_ansage",
                                         side_effect=freigeben),
              unittest.mock.patch.object(ansage.time, "sleep")):
            griff = ansage._sperre_holen("eigene")
        self.assertIsNotNone(griff)
        self.addCleanup(griff.close)
        self.assertEqual([True], verdraengt)

    def test_fremde_sitzung_wird_abgewartet_nicht_abgeschnitten(self):
        halter = ansage._sperre()
        self.besitzer(4242, "fremde")
        verdraengt = []
        takte = []

        def warten(_dauer):
            takte.append(1)
            if len(takte) == 3:                        # die fremde Ansage endet
                halter.close()

        with (unittest.mock.patch.object(ansage, "_verdraenge_laufende_ansage",
                                         side_effect=lambda: verdraengt.append(True)),
              unittest.mock.patch.object(ansage.time, "sleep", side_effect=warten)):
            griff = ansage._sperre_holen("eigene")
        self.assertIsNotNone(griff)
        self.addCleanup(griff.close)
        self.assertEqual([], verdraengt)               # nichts abgeschnitten
        self.assertEqual(3, len(takte))                # sondern angestanden

    def test_haengender_sprecher_haelt_die_schlange_nicht_ewig(self):
        halter = ansage._sperre()                      # bleibt belegt bis Testende
        self.addCleanup(halter.close)
        self.besitzer(4242, "fremde")
        uhr = iter([0.0, ansage.WARTESCHLANGE_FRIST_S + 1])
        with (unittest.mock.patch.object(ansage.time, "monotonic", side_effect=lambda: next(uhr)),
              unittest.mock.patch.object(ansage.time, "sleep")):
            self.assertIsNone(ansage._sperre_holen("eigene"))

    def test_besitzer_ohne_datei(self):
        self.assertEqual((0, ""), ansage._besitzer())


class Verdraengen(unittest.TestCase):
    """Die neue Ansage loest die laufende ab, statt zu schweigen."""

    def test_fremde_pid_wird_nicht_signalisiert(self):
        # PIDs werden wiederverwendet: ohne die cmdline-Pruefung bekaeme ein
        # beliebiger fremder Prozess ein SIGTERM.
        self.assertFalse(ansage._ist_ansage_prozess(os.getpid()))
        self.assertFalse(ansage._ist_ansage_prozess(1))
        self.assertFalse(ansage._ist_ansage_prozess(0))

    def test_verdraengen_ohne_pid_datei_tut_nichts(self):
        laufzeit = str(Path(self.enterContext(tempfile.TemporaryDirectory())))
        with unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": laufzeit}):
            ansage._verdraenge_laufende_ansage()        # darf nicht werfen

    def test_verdraengen_signalisiert_nur_echte_ansagen(self):
        laufzeit = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (laufzeit / "mimic-ansage.pid").write_text("4242\n", encoding="utf-8")
        gesendet = []
        with (unittest.mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(laufzeit)}),
              unittest.mock.patch.object(ansage, "_ist_ansage_prozess", return_value=True),
              unittest.mock.patch.object(ansage.os, "getpgid", return_value=4242),
              unittest.mock.patch.object(ansage.os, "killpg",
                                         side_effect=lambda pgid, sig: gesendet.append(pgid))):
            ansage._verdraenge_laufende_ansage()
        self.assertEqual([4242], gesendet)


class Einhaengen(unittest.TestCase):
    """Fremde Einstellungen sind heilig -- hier darf nichts verlorengehen."""

    def setUp(self):
        self.pfad = Path(tempfile.mkdtemp()) / "settings.json"
        self.programm = "/home/matthias/.local/bin/mimic-ansage"

    def schreiben(self, inhalt: str) -> None:
        self.pfad.write_text(inhalt, encoding="utf-8")

    def gelesen(self) -> dict:
        return json.loads(self.pfad.read_text(encoding="utf-8"))

    def test_legt_datei_an(self):
        code, meldung = ansage.einhaengen(self.pfad, self.programm)
        self.assertEqual(code, 0, meldung)
        haken = self.gelesen()["hooks"]
        self.assertEqual(list(haken), ["Stop", "Notification"])
        self.assertIn("mimic-ansage", haken["Stop"][0]["hooks"][0]["command"])

    def test_erhaelt_fremde_einstellungen(self):
        self.schreiben(json.dumps({
            "permissions": {"allow": ["Bash(uv run:*)"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "pruefen.sh"}]}]},
        }))
        code, meldung = ansage.einhaengen(self.pfad, self.programm)
        self.assertEqual(code, 0, meldung)
        wert = self.gelesen()
        self.assertEqual(wert["permissions"], {"allow": ["Bash(uv run:*)"]})
        self.assertEqual(wert["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "pruefen.sh")
        self.assertIn("Stop", wert["hooks"])

    def test_haengt_an_bestehende_stop_haken_an(self):
        self.schreiben(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "sonstwas.sh"}]}]}}))
        ansage.einhaengen(self.pfad, self.programm)
        stop = self.gelesen()["hooks"]["Stop"]
        self.assertEqual(len(stop), 2)
        self.assertEqual(stop[0]["hooks"][0]["command"], "sonstwas.sh")

    def test_zweimal_einhaengen_ergibt_einen_eintrag(self):
        ansage.einhaengen(self.pfad, self.programm)
        code, meldung = ansage.einhaengen(self.pfad, self.programm)
        self.assertEqual(code, 0)
        self.assertIn("schon eingehaengt", meldung)
        self.assertEqual(len(self.gelesen()["hooks"]["Stop"]), 1)

    def test_erkennt_sich_nach_pfadwechsel_wieder(self):
        ansage.einhaengen(self.pfad, "/alt/mimic-ansage")
        ansage.einhaengen(self.pfad, "/ganz/woanders/mimic-ansage")
        self.assertEqual(len(self.gelesen()["hooks"]["Stop"]), 1)

    def test_kaputtes_json_wird_nicht_angefasst(self):
        self.schreiben("{das ist kaputt")
        code, meldung = ansage.einhaengen(self.pfad, self.programm)
        self.assertEqual(code, 1)
        self.assertIn("kein gueltiges JSON", meldung)
        self.assertEqual(self.pfad.read_text(encoding="utf-8"), "{das ist kaputt")

    def test_unerwartete_struktur_wird_abgelehnt(self):
        self.schreiben(json.dumps({"hooks": {"Stop": "nein"}}))
        code, meldung = ansage.einhaengen(self.pfad, self.programm)
        self.assertEqual(code, 1)
        self.assertIn("keine Liste", meldung)

    def test_sicherung_bei_bestehender_datei(self):
        vorher = json.dumps({"model": "opus"})
        self.schreiben(vorher)
        ansage.einhaengen(self.pfad, self.programm)
        sicherung = self.pfad.with_suffix(".json.vor-ansage")
        self.assertEqual(sicherung.read_text(encoding="utf-8").strip(), vorher)

    def test_leere_datei(self):
        self.schreiben("")
        code, _ = ansage.einhaengen(self.pfad, self.programm)
        self.assertEqual(code, 0)
        self.assertIn("Stop", self.gelesen()["hooks"])


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


class SchalterTests(unittest.TestCase):
    """Der Schalter aus der Leiste muss den Sprechpfad selbst treffen.

    Eine Pruefung nur beim Hook-Aufruf reicht nicht: der Meldepfad startet sich
    abgekoppelt neu und kaeme sonst am Schalter vorbei.
    """

    def lauf(self, aus: bool):
        heim = Path(tempfile.mkdtemp())
        if aus:
            (heim / ".config/mimic").mkdir(parents=True)
            (heim / ".config/mimic/ansage-aus").touch()
        laufzeit = Path(tempfile.mkdtemp())
        ergebnis = subprocess.run(
            [sys.executable, str(ANSAGE_PY), "--sagen", "Probe", "--sitzung", "pruef"],
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/nonexistent", "HOME": str(heim), "XDG_RUNTIME_DIR": str(laufzeit)})
        self.assertEqual(ergebnis.returncode, 0)
        return (laufzeit / "mimic-ansage.log").read_text(encoding="utf-8")

    def test_schalter_aus_erreicht_den_sprechpfad(self):
        protokoll = self.lauf(aus=True)
        self.assertIn("abgeschaltet", protokoll)
        self.assertNotIn("spricht", protokoll)

    def test_ohne_schalter_wird_gesprochen(self):
        protokoll = self.lauf(aus=False)
        self.assertIn("spricht", protokoll)


if __name__ == "__main__":
    unittest.main()
