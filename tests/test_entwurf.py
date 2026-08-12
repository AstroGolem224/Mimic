"""Der Entwurfsreiter, soweit er ohne GPU pruefbar ist.

Der Generator selbst braucht eine 4-GB-Gewichtsdatei und eine eigene venv --
was hier laeuft, ist ein Stub, der dieselben JSON-Zeilen schreibt. Geprueft
wird die Mechanik drumherum: Torwaechter, Einsammeln der Kandidaten, und der
Fall, der im Betrieb wirklich vorkommt -- der Subprozess stirbt, ohne etwas
gemeldet zu haben.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class EntwurfTests(unittest.TestCase):
    def _stub(self, ordner: Path, rumpf: str) -> Path:
        weg = ordner / "stub-generator"
        weg.write_text("#!/bin/sh\n" + rumpf + "\n")
        weg.chmod(0o755)
        return weg

    def _lauf(self, rumpf: str, beschreibung="tiefe ruhige Stimme", text="Ein Satz.", anzahl=2):
        """Startet einen Entwurf gegen einen Stub und wartet auf dessen Ende."""
        from mimic import entwurf

        with tempfile.TemporaryDirectory() as ordner:
            heim = Path(ordner)
            alt = os.environ.get("XDG_DATA_HOME")
            os.environ["XDG_DATA_HOME"] = str(heim)
            stub = self._stub(heim, rumpf)
            try:
                with mock.patch.object(entwurf, "python_pfad", lambda: stub), \
                     mock.patch.object(entwurf, "skript_pfad", lambda: heim / "egal.py"):
                    lauf = entwurf.Entwurf()
                    lauf.starten(beschreibung, text, anzahl)
                    for _ in range(500):
                        stand = lauf.stand()
                        if not stand["laeuft"]:
                            break
                        time.sleep(0.01)
                    lauf.schliessen()
                    return stand
            finally:
                if alt is None:
                    os.environ.pop("XDG_DATA_HOME", None)
                else:
                    os.environ["XDG_DATA_HOME"] = alt

    def test_leere_eingaben_kommen_nicht_bis_zum_subprozess(self):
        from mimic.entwurf import MAX_KANDIDATEN, Entwurf

        lauf = Entwurf()
        for beschreibung, text, anzahl, erwartet in [
            ("", "satz", 1, "Beschreibung"),
            ("   ", "satz", 1, "Beschreibung"),
            ("tief", "  ", 1, "Probesatz"),
            ("tief", "satz", 0, "Kandidaten"),
            ("tief", "satz", MAX_KANDIDATEN + 1, "Kandidaten"),
        ]:
            with self.assertRaises(ValueError) as gefangen:
                lauf.starten(beschreibung, text, anzahl)
            self.assertIn(erwartet, str(gefangen.exception))

    def test_fehlende_umgebung_wird_benannt_statt_zu_starten(self):
        from mimic import entwurf

        with tempfile.TemporaryDirectory() as ordner:
            with mock.patch.object(entwurf, "python_pfad", lambda: Path(ordner) / "gibtsnicht"):
                with self.assertRaises(RuntimeError) as gefangen:
                    entwurf.Entwurf().starten("tief", "Ein Satz.", 1)
        self.assertIn("setup --entwurf", str(gefangen.exception))

    def test_gemeldete_kandidaten_landen_im_stand(self):
        stand = self._lauf(
            'echo \'{"kind":"laden","geraet":"cuda"}\'\n'
            'echo \'{"kind":"kandidat","nummer":0,"datei":"/tmp/a.wav","dauer":9.4}\'\n'
            'echo \'{"kind":"kandidat","nummer":1,"datei":"/tmp/b.wav","dauer":8.1}\'\n'
            "echo 'irgendein tqdm-Balken ohne JSON'\n"
            'echo \'{"kind":"fertig"}\'\n')
        self.assertFalse(stand["laeuft"])
        self.assertEqual("", stand["fehler"])
        self.assertEqual([0, 1], [k["nummer"] for k in stand["kandidaten"]])
        self.assertEqual(9.4, stand["kandidaten"][0]["dauer"])
        self.assertEqual("fertig", stand["phase"])

    def test_geschwaetziges_stderr_blockiert_den_generator_nicht(self):
        """Der Fehler, der die erste Fassung zum Stillstand brachte.

        transformers und tqdm schreiben nach stderr. Landet das in einer
        zweiten Pipe, die niemand liest, blockiert das Kind nach 64 KB in
        write() -- sichtbar als wchan=anon_pipe_write, und zwar fuer immer.
        Hier sind es 400 KB, also das Sechsfache des Puffers.
        """
        stand = self._lauf(
            "i=0\n"
            "while [ $i -lt 4000 ]; do\n"
            "  echo 'Fetching 18 files: 44%|####      | 8/18 [01:09<01:27,  8.74s/it]' >&2\n"
            "  i=$((i+1))\n"
            "done\n"
            'echo \'{"kind":"kandidat","nummer":0,"datei":"/tmp/a.wav","dauer":9.4}\'\n'
            'echo \'{"kind":"fertig"}\'\n')
        self.assertFalse(stand["laeuft"], "der Generator haengt an der vollen Pipe")
        self.assertEqual([0], [k["nummer"] for k in stand["kandidaten"]])
        self.assertEqual("", stand["fehler"])

    def test_stiller_tod_des_generators_wird_zum_grund_aus_stderr(self):
        """Der haeufigste Betriebsfall: OOM-Kill oder fehlendes Paket.

        Das Skript kommt dann nicht mehr dazu, einen Fehler zu melden. Ohne
        stderr staende das Fenster vor 'nichts passiert'.
        """
        stand = self._lauf("echo 'ModuleNotFoundError: transformers' >&2\nexit 1")
        self.assertFalse(stand["laeuft"])
        self.assertIn("transformers", stand["fehler"])
        self.assertEqual([], stand["kandidaten"])

    def test_zweiter_start_waehrend_eines_laufs_wird_abgelehnt(self):
        from mimic import entwurf

        with tempfile.TemporaryDirectory() as ordner:
            heim = Path(ordner)
            alt = os.environ.get("XDG_DATA_HOME")
            os.environ["XDG_DATA_HOME"] = str(heim)
            stub = self._stub(heim, "sleep 5")
            try:
                with mock.patch.object(entwurf, "python_pfad", lambda: stub), \
                     mock.patch.object(entwurf, "skript_pfad", lambda: heim / "egal.py"):
                    lauf = entwurf.Entwurf()
                    lauf.starten("tief", "Ein Satz.", 1)
                    with self.assertRaises(RuntimeError) as gefangen:
                        lauf.starten("tief", "Ein Satz.", 1)
                    self.assertIn("laeuft schon", str(gefangen.exception))
                    lauf.schliessen()
            finally:
                if alt is None:
                    os.environ.pop("XDG_DATA_HOME", None)
                else:
                    os.environ["XDG_DATA_HOME"] = alt

    def test_entwerfen_liegt_neben_entwurf_und_zieht_kein_mimic(self):
        """entwerfen.py laeuft in einer fremden Umgebung -- ein Import aus dem
        Paket waere dort ein ModuleNotFoundError, und zwar erst zur Laufzeit."""
        from mimic.entwurf import skript_pfad

        quelle = skript_pfad().read_text()
        self.assertTrue(skript_pfad().is_file())
        for zeile in quelle.splitlines():
            nackt = zeile.strip()
            self.assertFalse(nackt.startswith(("from .", "from mimic", "import mimic")),
                             f"entwerfen.py importiert aus dem Paket: {nackt}")


if __name__ == "__main__":
    unittest.main()
