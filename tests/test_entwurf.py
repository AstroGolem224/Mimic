"""Der Entwurfsreiter, soweit er ohne GPU pruefbar ist.

Der Generator selbst braucht eine 4-GB-Gewichtsdatei und eine eigene venv --
was hier laeuft, ist ein Stub, der dieselben JSON-Zeilen schreibt. Geprueft
wird die Mechanik drumherum: Torwaechter, Einsammeln der Kandidaten, und der
Fall, der im Betrieb wirklich vorkommt -- der Subprozess stirbt, ohne etwas
gemeldet zu haben.
"""

from __future__ import annotations

import os
import subprocess
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
                with mock.patch.object(entwurf, "python_pfad", lambda motor=None: stub), \
                     mock.patch.object(entwurf, "skript_pfad", lambda motor=None: heim / "egal.py"):
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
            with mock.patch.object(entwurf, "python_pfad", lambda motor=None: Path(ordner) / "gibtsnicht"):
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
                with mock.patch.object(entwurf, "python_pfad", lambda motor=None: stub), \
                     mock.patch.object(entwurf, "skript_pfad", lambda motor=None: heim / "egal.py"):
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

    def test_motorskripte_ziehen_kein_mimic(self):
        """Die Motorskripte laufen in fremden Umgebungen -- ein Import aus dem
        Paket waere dort ein ModuleNotFoundError, und zwar erst zur Laufzeit."""
        from mimic.entwurf import EINDEUTSCHER, EINDEUTSCHER_V2, MOTOREN, skript_pfad

        # Der Eindeutscher steht nicht in MOTOREN, laeuft aber in derselben
        # Fremdumgebung und faellt sonst durch dieses Netz.
        for name in (*MOTOREN, EINDEUTSCHER.name, EINDEUTSCHER_V2.name):
            pfad = skript_pfad(name)
            self.assertTrue(pfad.is_file(), f"{pfad} fehlt")
            for zeile in pfad.read_text().splitlines():
                nackt = zeile.strip()
                self.assertFalse(nackt.startswith(("from .", "from mimic", "import mimic")),
                                 f"{pfad.name} importiert aus dem Paket: {nackt}")

    def test_jeder_motor_hat_eigene_umgebung_und_skript(self):
        """Zusammengelegte venvs waeren der Pin-Konflikt, den der Spike gezeigt hat."""
        from mimic.entwurf import MOTOREN, VORGABE_MOTOR, venv_pfad

        self.assertIn(VORGABE_MOTOR, MOTOREN)
        pfade = {venv_pfad(name) for name in MOTOREN}
        self.assertEqual(len(pfade), len(MOTOREN), "zwei Motoren teilen sich eine venv")
        skripte = {MOTOREN[name].skript for name in MOTOREN}
        self.assertEqual(len(skripte), len(MOTOREN), "zwei Motoren teilen sich ein Skript")

    def test_eindeutscher_teilt_sich_nichts_mit_den_motoren(self):
        """Eigene venv und eigenes Skript: transformers 5.0.0 vertraegt sich
        weder mit qwen-tts (<= 4.57.6) noch mit voxcpm."""
        from mimic.entwurf import EINDEUTSCHER, MOTOREN, venv_pfad

        self.assertNotIn(EINDEUTSCHER.name, MOTOREN, "der Eindeutscher ist kein Entwurfsmotor")
        self.assertNotIn(venv_pfad(EINDEUTSCHER.name), {venv_pfad(n) for n in MOTOREN})
        self.assertNotIn(EINDEUTSCHER.skript, {MOTOREN[n].skript for n in MOTOREN})

    def test_eindeutscher_v2_laesst_version_1_unveraendert(self):
        from mimic.entwurf import EINDEUTSCHER, EINDEUTSCHER_V2, MOTOREN, venv_pfad

        self.assertNotEqual(EINDEUTSCHER.name, EINDEUTSCHER_V2.name)
        self.assertNotEqual(EINDEUTSCHER.skript, EINDEUTSCHER_V2.skript)
        self.assertNotEqual(venv_pfad(EINDEUTSCHER.name), venv_pfad(EINDEUTSCHER_V2.name))
        self.assertNotIn(EINDEUTSCHER_V2.name, MOTOREN)

    def test_unbekannter_motor_wird_abgelehnt(self):
        from mimic.entwurf import Entwurf

        with self.assertRaises(ValueError) as gefangen:
            Entwurf().starten("tief", "Ein Satz.", 1, motor="gibtsnicht")
        self.assertIn("unbekannter Motor", str(gefangen.exception))

    def test_zu_langer_probesatz_faellt_vor_dem_gpu_lauf_durch(self):
        """Sonst rechnet das Modell eine Minute fuer ein Profil, das nie laedt.

        Der Probesatz wird woertlich das ref.txt, und load_voice lehnt mehr als
        MAX_TEXT_BYTES ab. Die Pruefung gehoert deshalb vor den Start, nicht
        hinter das Ergebnis.
        """
        from mimic.entwurf import Entwurf
        from mimic.voices import MAX_TEXT_BYTES

        with self.assertRaises(ValueError) as gefangen:
            Entwurf().starten("tief", "ä" * MAX_TEXT_BYTES, 1)
        self.assertIn("zu lang", str(gefangen.exception))

    def test_prozessgruppe_wird_ganz_beendet(self):
        """terminate() traf frueher nur das Kind. Torch und die HF-Downloader
        starten Enkel, die danach weiterliefen und VRAM festhielten."""
        from mimic import entwurf

        with tempfile.TemporaryDirectory() as ordner:
            heim = Path(ordner)
            alt = os.environ.get("XDG_DATA_HOME")
            os.environ["XDG_DATA_HOME"] = str(heim)
            marke = heim / "enkel-lebt"
            # Das Kind startet einen Enkel, der eine Datei anlegt und dann lange
            # schlaeft. Ueberlebt der Enkel das Abbrechen, laeuft er weiter.
            # Eindeutige Marke im Kommando: ein blosses "sleep 30" traefe per
            # pgrep auch fremde Prozesse auf der Maschine und machte den Test
            # von der Laune des Systems abhaengig.
            kennung = f"mimic-test-{os.getpid()}"
            stub = self._stub(heim, f"sh -c 'echo da > {marke}; exec sleep 3000 {kennung}' &\n"
                                    f"sleep 3000 {kennung}")
            try:
                with mock.patch.object(entwurf, "python_pfad", lambda motor=None: stub), \
                     mock.patch.object(entwurf, "skript_pfad", lambda motor=None: heim / "egal.py"):
                    lauf = entwurf.Entwurf()
                    lauf.starten("tief", "Ein Satz.", 1)
                    for _ in range(200):
                        if marke.exists():
                            break
                        time.sleep(0.01)
                    self.assertTrue(marke.exists(), "der Enkel ist nie gestartet")
                    enkel = subprocess.run(["pgrep", "-f", kennung],
                                           capture_output=True, text=True).stdout.split()
                    lauf.abbrechen()
                    time.sleep(0.5)
                    uebrig = subprocess.run(["pgrep", "-f", kennung],
                                            capture_output=True, text=True).stdout.split()
                    self.assertFalse(set(enkel) & set(uebrig),
                                     "ein Enkelprozess hat den Abbruch ueberlebt")
                    lauf.schliessen()
            finally:
                if alt is None:
                    os.environ.pop("XDG_DATA_HOME", None)
                else:
                    os.environ["XDG_DATA_HOME"] = alt


class StimmenwurzelTests(unittest.TestCase):
    """Die Symlink-Sicherung in load_voice begann frueher erst UNTERHALB des
    Stimmenverzeichnisses. Ein Symlink an `voices` selbst wurde verfolgt, und
    ein Zielverzeichnis mit Modus 0700 bestand danach jede weitere Pruefung."""

    def test_stimmenverzeichnis_als_symlink_wird_abgelehnt(self):
        from mimic.voices import VoiceError, load_voice

        with tempfile.TemporaryDirectory() as ordner:
            heim = Path(ordner)
            echt = heim / "echt"
            profil = echt / "stimme"
            profil.mkdir(parents=True)
            echt.chmod(0o700)
            profil.chmod(0o700)
            (profil / "ref.txt").write_text("egal")
            (profil / "ref.txt").chmod(0o600)
            (profil / "ref.wav").write_bytes(b"RIFF")
            (profil / "ref.wav").chmod(0o600)
            gefaelscht = heim / "voices"
            gefaelscht.symlink_to(echt)

            with self.assertRaises(VoiceError) as gefangen:
                load_voice("stimme", gefaelscht)
            # Der Grund ist zweitrangig -- entscheidend ist, dass dem Symlink
            # nicht gefolgt wird und kein Profil herauskommt.
            self.assertIn(gefangen.exception.reason,
                          ("unknown_voice", "invalid_voice_profile"))


class KoerperTypenTests(unittest.TestCase):
    """`bool("false")` ist True. Ein Koerper mit {"force": "false"} haette
    deshalb ein bestehendes Stimmprofil ueberschrieben."""

    def _handler(self):
        from mimic.gui import _GuiHandler
        return _GuiHandler

    def test_force_nimmt_nur_echte_wahrheitswerte(self):
        handler = self._handler()
        self.assertIs(False, handler._feld_ja({}, "force"))
        self.assertIs(True, handler._feld_ja({"force": True}, "force"))
        for boese in ("false", "true", 0, 1, None, [], {}):
            with self.assertRaises(ValueError, msg=f"{boese!r} wurde angenommen"):
                handler._feld_ja({"force": boese}, "force")

    def test_text_und_zahl_werden_nicht_umgebogen(self):
        handler = self._handler()
        self.assertEqual("hallo", handler._feld_text({"name": "hallo"}, "name"))
        for boese in (1, None, ["a"], {"a": 1}, True):
            with self.assertRaises(ValueError, msg=f"{boese!r} wurde zu Text"):
                handler._feld_text({"name": boese}, "name")
        self.assertEqual(3, handler._feld_zahl({"anzahl": 3}, "anzahl", 1))
        for boese in ("3", 3.0, None, True):
            with self.assertRaises(ValueError, msg=f"{boese!r} wurde zur Zahl"):
                handler._feld_zahl({"anzahl": boese}, "anzahl", 1)


if __name__ == "__main__":
    unittest.main()


class WorkerWacheTests(unittest.TestCase):
    """Der schwerste Befund aus dem Codex-Review vom 2026-08-12.

    Die Frist in _execute prueft erst, NACHDEM generate_stream einen Chunk
    geliefert hat. Blockiert schon der Aufruf selbst, greift nichts -- und der
    Worker ist der einzige Modellbesitzer. Er nimmt weiter Auftraege an, die
    dann fuer immer warten.
    """

    def test_haengender_modellaufruf_reisst_den_worker_ab(self):
        import threading
        from unittest import mock

        from mimic import worker

        engine = worker.Engine.__new__(worker.Engine)
        engine.fatal = threading.Event()

        fertig = threading.Event()
        with mock.patch.object(worker, "REQUEST_TIMEOUT", 0.05), \
             mock.patch.object(worker, "WACHE_ZUSCHLAG_S", 0.0), \
             mock.patch.object(worker, "wake_listener") as geweckt:
            wache = threading.Thread(target=engine._wache, args=(fertig, "sprechen"), daemon=True)
            wache.start()
            wache.join(timeout=5)
        self.assertFalse(wache.is_alive(), "die Wache haengt selbst")
        self.assertTrue(engine.fatal.is_set(), "fatal wurde nicht gesetzt")
        self.assertTrue(geweckt.called, "der Listener wurde nicht geweckt")

    def test_ein_fertiger_lauf_loest_die_wache_nicht_aus(self):
        """Ein langsamer, aber lebendiger Lauf darf den Worker nie abreissen."""
        import threading
        from unittest import mock

        from mimic import worker

        engine = worker.Engine.__new__(worker.Engine)
        engine.fatal = threading.Event()

        fertig = threading.Event()
        with mock.patch.object(worker, "REQUEST_TIMEOUT", 5.0), \
             mock.patch.object(worker, "WACHE_ZUSCHLAG_S", 0.0), \
             mock.patch.object(worker, "wake_listener") as geweckt:
            wache = threading.Thread(target=engine._wache, args=(fertig, "sprechen"), daemon=True)
            wache.start()
            fertig.set()                     # so, wie es der finally-Zweig tut
            wache.join(timeout=5)
        self.assertFalse(engine.fatal.is_set(), "die Wache hat einen gesunden Lauf abgerissen")
        self.assertFalse(geweckt.called)


class QwenRuntimeTests(unittest.TestCase):
    def test_qwen_nimmt_original_statt_klon_vom_klon(self):
        from mimic.worker import QwenRuntime

        with tempfile.TemporaryDirectory() as ordner:
            profil = Path(ordner)
            ref = profil / "ref.wav"
            original = profil / "qwen-source.wav"
            ref.touch()
            self.assertEqual(str(ref), QwenRuntime._referenzpfad(str(ref)))
            original.touch()
            self.assertEqual(str(original), QwenRuntime._referenzpfad(str(ref)))

    def test_qwen_loest_den_voice_fd_fuer_seinen_kindprozess_auf(self):
        from mimic.worker import QwenRuntime

        with tempfile.TemporaryDirectory() as ordner:
            ref = Path(ordner) / "ref.wav"
            ref.touch()
            with ref.open("rb") as handle:
                fd_pfad = f"/proc/self/fd/{handle.fileno()}"
                self.assertEqual(str(ref), QwenRuntime._referenzpfad(fd_pfad))


class ZahlwortTests(unittest.TestCase):
    """dots.tts normalisiert nur zh und en. Deutsche Ziffern kamen roh im
    Modell an: "1241" wurde als "flossweineinunswansich" gesprochen."""

    def test_zahlwoerter(self):
        from mimic.voices import zahlwort

        for zahl, wort in [
            (0, "null"), (1, "eins"), (7, "sieben"), (12, "zwoelf"), (16, "sechzehn"),
            (21, "einundzwanzig"), (30, "dreissig"), (47, "siebenundvierzig"),
            (100, "einhundert"), (101, "einhunderteins"), (164, "einhundertvierundsechzig"),
            (1000, "eintausend"), (1241, "eintausendzweihunderteinundvierzig"),
            (2026, "zweitausendsechsundzwanzig"),
            (1_000_000, "eine Million"), (2_000_000, "zwei Millionen"),
        ]:
            self.assertEqual(wort, zahlwort(zahl), f"{zahl}")

    def test_saetze(self):
        from mimic.voices import sprich_zahlen

        for roh, gesprochen in [
            # Der Fall aus dem Betrieb: Testzaehler.
            ("1241 passed, 0 failed.", "eintausendzweihunderteinundvierzig passed, null failed."),
            # Kennungen sind keine Zahlen -- "sechs punkt drei", nicht "dreiundsechzig".
            ("T-6.3 committet.", "T sechs punkt drei committet."),
            ("Version 0.9.3 ist raus.", "Version null punkt neun punkt drei ist raus."),
            # Zahl am Satzende und vor Komma: die fielen in der ersten Fassung durch.
            ("Sektor 12, Druck bei 3,4 bar.", "Sektor zwoelf, Druck bei drei komma vier bar."),
            ("Kriterium B: 12/12.", "Kriterium B: zwoelf/zwoelf."),
            # Tausenderpunkt sieht aus wie eine Kennung, ist aber eine Zahl.
            ("1.241 Zeilen.", "eintausendzweihunderteinundvierzig Zeilen."),
        ]:
            self.assertEqual(gesprochen, sprich_zahlen(roh), f"{roh!r}")

    def test_text_ohne_ziffern_bleibt_unberuehrt(self):
        from mimic.voices import sprich_zahlen

        for satz in ["Der Weg ist frei.", "Kein Wort mit Ziffern.", ""]:
            self.assertEqual(satz, sprich_zahlen(satz))
