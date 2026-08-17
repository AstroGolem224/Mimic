from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import array
import tempfile
import threading
import time
import unittest
import unittest.mock
import wave
from pathlib import Path

from mimic.protocol import encode_frame, finish_chunks, json_frame, read_frame, write_chunk


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float = 2):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class StubWorker:
    def __init__(self, path: Path):
        from http.server import BaseHTTPRequestHandler
        from mimic.frontend import ThreadingUnixServer

        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                owner.requests.append(request)
                text = request["text"]
                if text == "busy":
                    body = b'{"reason":"busy","message":"voll"}'
                    self.send_response(429)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if text == "low-vram":
                    body = b'{"reason":"insufficient_vram","message":"knapp"}'
                    self.send_response(503)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.mimic.frames")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                if text == "timeout":
                    time.sleep(0.35)
                    return
                head = {"v": 1, "sample_rate": 48000, "channels": 1, "format": "s16le",
                        "request_id": "stub-id", "mode": request["mode"], "voice": request["voice"]}
                try:
                    write_chunk(self.wfile, json_frame("H", head))
                    if text == "pre-cancel":
                        owner.pre_cancel_ready.set()
                        self.connection.settimeout(1)
                        if self.connection.recv(1) == b"":
                            owner.pre_cancelled.set()
                        return
                    if text == "cancel":
                        for number in range(100):
                            owner.generated = number + 1
                            write_chunk(self.wfile, encode_frame("A", b"\0\0" * 32))
                            time.sleep(0.02)
                    else:
                        write_chunk(self.wfile, encode_frame("A", b"\0\0" * 64))
                        write_chunk(self.wfile, json_frame("E", {"status": "ok", "samples": 64}))
                        finish_chunks(self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    owner.cancelled.set()

        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.server = ThreadingUnixServer(str(path), Handler)
        self.requests = []
        self.generated = 0
        self.cancelled = threading.Event()
        self.pre_cancel_ready = threading.Event()
        self.pre_cancelled = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def create_voice(root: Path, name: str = "matthias") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    root.chmod(0o700)
    directory.chmod(0o700)
    with wave.open(str(directory / "ref.wav"), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(b"\0\0" * (48_000 * 3))
    (directory / "ref.txt").write_text("Dies ist Matthias.", encoding="utf-8")
    (directory / "ref.wav").chmod(0o600)
    (directory / "ref.txt").chmod(0o600)
    return directory


class Phase1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.voices = cls.root / "voices"
        create_voice(cls.voices)
        cls.front_socket = cls.root / "frontend.socket"
        cls.worker_socket = cls.root / "worker.socket"
        # Umgebung und Fristen werden global veraendert -- beides muss in
        # tearDownClass zurueck. Sonst erben spaetere Suites im selben Prozess
        # den Temp-Socket, ein falsches XDG_RUNTIME_DIR (womit `systemctl --user`
        # den Bus nicht mehr findet) und eine 0.1-s-Frist, gegen die kein echtes
        # Modell laden kann. Genau daran sind die GPU-Tests zuerst gescheitert.
        cls.saved_env = {key: os.environ.get(key) for key in
                         ("MIMIC_VOICES_DIR", "MIMIC_SOCKET", "MIMIC_WORKER_SOCKET",
                          "XDG_RUNTIME_DIR")}
        os.environ["MIMIC_VOICES_DIR"] = str(cls.voices)
        os.environ["MIMIC_SOCKET"] = str(cls.front_socket)
        os.environ["MIMIC_WORKER_SOCKET"] = str(cls.worker_socket)
        os.environ["XDG_RUNTIME_DIR"] = str(cls.root)
        from mimic import frontend
        cls.saved_timeouts = {name: getattr(frontend, name) for name in
                              ("FIRST_AUDIO_TIMEOUT", "HEADER_TIMEOUT", "FRAME_TIMEOUT")}
        frontend.FIRST_AUDIO_TIMEOUT = 0.1
        frontend.HEADER_TIMEOUT = 0.1
        frontend.FRAME_TIMEOUT = 0.2
        cls.frontend_module = frontend
        cls.stub = StubWorker(cls.worker_socket)
        cls.server = frontend._server(frontend.FrontendHandler, cls.front_socket)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.stub.close()
        cls.temp.cleanup()
        for name, value in cls.saved_timeouts.items():
            setattr(cls.frontend_module, name, value)
        for key, value in cls.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def post(self, value: dict | bytes):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        conn = UnixConnection(self.front_socket)
        conn.request("POST", "/speak", body, {"Content-Length": str(len(body)),
                                               "Content-Type": "application/json"})
        return conn, conn.getresponse()

    def assert_reason(self, value, status, reason):
        conn, response = self.post(value)
        self.assertEqual(status, response.status)
        self.assertEqual(reason, json.loads(response.read())["reason"])
        response.close()
        conn.close()

    def test_01_frontend_imports_no_torch(self):
        import sys
        self.assertNotIn("torch", sys.modules)

    def test_02_protocol_contract(self):
        conn, response = self.post({"text": "Hallo", "voice": "matthias", "mode": "mf"})
        self.assertEqual(200, response.status)
        frames = []
        while True:
            kind, payload = read_frame(response)
            frames.append((kind, payload))
            if kind == "E":
                break
        self.assertEqual(["H", "A", "E"], [kind for kind, _ in frames])
        head = json.loads(frames[0][1])
        self.assertEqual({"v", "sample_rate", "channels", "format", "request_id", "mode", "voice"},
                         set(head))
        self.assertEqual("ok", json.loads(frames[-1][1])["status"])
        response.close()
        conn.close()

    def test_03_all_pre_stream_reasons(self):
        self.assert_reason(b"{", 400, "bad_request")
        self.assert_reason({"text": "x" * 1001}, 400, "text_too_long")
        self.assert_reason({"text": "x", "voice": "missing"}, 404, "unknown_voice")
        broken = self.voices / "broken"
        broken.mkdir(exist_ok=True)
        broken.chmod(0o700)
        (broken / "ref.txt").write_text("x")
        (broken / "ref.txt").chmod(0o600)
        self.assert_reason({"text": "x", "voice": "broken"}, 422, "invalid_voice_profile")
        self.assert_reason({"text": "busy"}, 429, "busy")
        self.assert_reason({"text": "low-vram"}, 503, "insufficient_vram")
        self.assert_reason({"text": "timeout"}, 504, "worker_timeout")
        old = self.frontend_module.worker_socket_path
        self.frontend_module.worker_socket_path = lambda: self.root / "does-not-exist"
        try:
            self.assert_reason({"text": "x"}, 503, "worker_unavailable")
        finally:
            self.frontend_module.worker_socket_path = old

    def test_04_path_traversal_and_symlink(self):
        before = len(self.stub.requests)
        for voice in ("../matthias", "/etc", "has.dot", "A"):
            self.assert_reason({"text": "x", "voice": voice}, 404, "unknown_voice")
        evil = self.voices / "evil"
        evil.mkdir(exist_ok=True)
        evil.chmod(0o700)
        (evil / "ref.wav").symlink_to(self.voices / "matthias/ref.wav")
        (evil / "ref.txt").write_text("nicht lesen")
        (evil / "ref.txt").chmod(0o600)
        self.assert_reason({"text": "x", "voice": "evil"}, 422, "invalid_voice_profile")
        self.assertEqual(before, len(self.stub.requests))

    def test_05_limits_before_worker(self):
        before = len(self.stub.requests)
        self.assert_reason({"text": "x" * 1001}, 400, "text_too_long")
        # Regler ausserhalb der Grenzen melden statt still zu klemmen -- wer 1.5
        # schickt, hat sich vertan und soll das erfahren. Der Worker haelt seine
        # eigene Klemme trotzdem, das hier ist die Meldung, nicht der Schutz.
        self.assert_reason({"text": "x", "hall": 1.5}, 400, "bad_request")
        self.assert_reason({"text": "x", "hall": "viel"}, 400, "bad_request")
        conn = UnixConnection(self.front_socket)
        huge = b"x" * (64 * 1024 + 1)
        try:
            conn.request("POST", "/speak", huge, {"Content-Length": str(len(huge))})
        except BrokenPipeError:
            # Genau das gewuenschte Verhalten: das Frontend lehnt allein nach
            # Content-Length ab und schliesst, ohne 64 KiB leerzulesen, die es
            # schon verworfen hat. Ob der Client seinen Rumpf davor noch
            # fertigschreiben kann, ist Zeitfrage -- die Antwort steht trotzdem
            # im Socketpuffer und wird unten geprueft.
            pass
        response = conn.getresponse()
        self.assertEqual("bad_request", json.loads(response.read())["reason"])
        response.close()
        conn.close()
        self.assertEqual(before, len(self.stub.requests))
        self.assert_reason({"text": "busy"}, 429, "busy")

        # Auch die echte Worker-Warteschlange lehnt den fuenften Wartenden ab,
        # ohne einen Ladepfad zu betreten; mf wird vor bereits wartendem soar gezogen.
        import itertools
        import heapq
        import threading
        from mimic import worker
        engine = worker.Engine.__new__(worker.Engine)
        engine.jobs = []
        engine.sequence = itertools.count()
        engine.runtimes = {}
        engine.state = "cold"
        engine.state_lock = threading.Lock()
        engine.condition = threading.Condition(engine.state_lock)
        engine.write_status = lambda *_args: None
        soar = engine.submit({"text": "x", "voice": "matthias", "mode": "soar"})
        mf = engine.submit({"text": "x", "voice": "matthias", "mode": "mf"})
        self.assertIs(mf, heapq.heappop(engine.jobs))
        self.assertIs(soar, heapq.heappop(engine.jobs))
        for _ in range(worker.MAX_WAITING):
            engine.submit({"text": "x", "voice": "matthias", "mode": "soar"})
        with self.assertRaises(worker.WorkerRefusal) as raised:
            engine.submit({"text": "x", "voice": "matthias", "mode": "mf"})
        self.assertEqual("busy", raised.exception.reason)

    def test_06_disconnect_stops_generation(self):
        self.stub.cancelled.clear()
        conn, response = self.post({"text": "cancel"})
        self.assertEqual("H", read_frame(response)[0])
        self.assertEqual("A", read_frame(response)[0])
        response.close()
        conn.close()
        self.assertTrue(self.stub.cancelled.wait(1.5))
        stopped_at = self.stub.generated
        time.sleep(0.1)
        self.assertEqual(stopped_at, self.stub.generated)
        self.assertLess(stopped_at, 100)

    def test_07_vram_gate_prevents_load(self):
        from mimic import worker
        marker = self.root / "loaded"
        os.environ["MIMIC_VRAM_FREE_MIB"] = "7999"
        os.environ["MIMIC_FAKE_LOAD_MARKER"] = str(marker)
        try:
            engine = worker.Engine.__new__(worker.Engine)
            with self.assertRaises(worker.WorkerRefusal) as raised:
                engine._load("mf")
            self.assertEqual("insufficient_vram", raised.exception.reason)
            self.assertFalse(marker.exists())
        finally:
            os.environ.pop("MIMIC_VRAM_FREE_MIB", None)
            os.environ.pop("MIMIC_FAKE_LOAD_MARKER", None)

    def test_07b_vram_gatter_ohne_cuda_kontext(self):
        """Ist der VRAM erschoepft, wirft torch.cuda.mem_get_info() -- der Aufruf
        legt selbst einen CUDA-Kontext von einigen hundert MB an. Gemessen
        2026-08-16 bei 273 MiB frei: Mimic meldete `worker_unavailable` statt
        des dafuer gedachten `insufficient_vram`."""
        import sys
        from mimic import worker
        os.environ.pop("MIMIC_VRAM_FREE_MIB", None)
        engine = worker.Engine.__new__(worker.Engine)
        with unittest.mock.patch.object(worker, "_nvidia_smi_free_mib", return_value=273), \
                unittest.mock.patch.dict(sys.modules, {"torch": None}):
            with self.assertRaises(worker.WorkerRefusal) as raised:
                engine._load("mf")
        self.assertEqual("insufficient_vram", raised.exception.reason)
        self.assertIn("273", raised.exception.message)

    def test_08_disconnect_vor_erstem_rahmen_schliesst_worker(self):
        self.stub.pre_cancel_ready.clear()
        self.stub.pre_cancelled.clear()
        body = json.dumps({"text": "pre-cancel"}).encode()
        consumer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        consumer.connect(str(self.front_socket))
        consumer.sendall(
            b"POST /speak HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.assertTrue(self.stub.pre_cancel_ready.wait(0.5))
        consumer.shutdown(socket.SHUT_RDWR)
        consumer.close()
        self.assertTrue(self.stub.pre_cancelled.wait(0.5))


if __name__ == "__main__":
    unittest.main()


class TextAndLevelTests(unittest.TestCase):
    """Die zwei Stellen der Ausgabeaufbereitung, die still falsch sein koennen."""

    def test_10_kurze_fragmente_werden_nicht_allein_synthetisiert(self):
        from mimic.voices import MIN_SATZ_ZEICHEN, split_sentences
        # Phase 0: dots.tts halluziniert bei Fragmenten ohne Satzkontext
        # ("ähhh gemerdscht"). Jedes Stueck muss also Satzlaenge haben --
        # ausser es gibt nur eines.
        for text in ("Ja. Das ist ein vollstaendiger Satz mit genug Inhalt.",
                     "Ein vollstaendiger Satz mit Inhalt. Ok. Nein.",
                     "Wirklich? Ja! Und zwar sofort, ohne Wenn und Aber."):
            teile = split_sentences(text)
            self.assertTrue(teile, text)
            if len(teile) > 1:
                for teil in teile:
                    self.assertGreaterEqual(len(teil), MIN_SATZ_ZEICHEN, f"{teil!r} aus {text!r}")
            # Nichts darf verlorengehen.
            self.assertEqual(sorted(text.split()), sorted(" ".join(teile).split()))

    def test_10a_lange_stuecke_werden_begrenzt(self):
        from mimic.voices import MAX_SATZ_ZEICHEN, MIN_SATZ_ZEICHEN, split_sentences
        text = " ".join(["Dieser lange Text enthaelt viele Woerter und ein Komma,"] * 8)
        teile = split_sentences(text)
        self.assertGreater(len(teile), 1)
        for teil in teile:
            self.assertGreaterEqual(len(teil), MIN_SATZ_ZEICHEN)
            self.assertLessEqual(len(teil), MAX_SATZ_ZEICHEN + MIN_SATZ_ZEICHEN)
        self.assertEqual(sorted(text.split()), sorted(" ".join(teile).split()))

    def test_10b_normale_saetze_bleiben_unveraendert(self):
        from mimic.voices import split_sentences
        text = ("Dieser erste Satz ist lang genug und bleibt unveraendert. "
                "Auch dieser zweite Satz hat genug Inhalt und bleibt ganz.")
        self.assertEqual([
            "Dieser erste Satz ist lang genug und bleibt unveraendert.",
            "Auch dieser zweite Satz hat genug Inhalt und bleibt ganz.",
        ], split_sentences(text))

    def test_10d_satz_ohne_klauselgrenze_bleibt_ganz(self):
        from mimic.voices import MAX_SATZ_HART, MAX_SATZ_ZEICHEN, split_sentences
        text = ("Der alte Kartograph zeichnete die verwitterte Karte des noerdlichen "
                "Gebirges bei flackerndem Kerzenlicht vollstaendig neu")
        self.assertGreater(len(text), MAX_SATZ_ZEICHEN)
        self.assertLessEqual(len(text), MAX_SATZ_HART)
        # Kein Schnitt am blossen Leerzeichen mitten im Satz.
        self.assertEqual([text], split_sentences(text))

    def test_10e_klauselgrenze_hinter_der_zielmarke_wird_genutzt(self):
        from mimic.voices import MAX_SATZ_ZEICHEN, split_sentences
        vorn = ("Der alte Kartograph zeichnete die verwitterte Karte des "
                "noerdlichen Gebirges vollstaendig neu,")
        hinten = "und niemand im Dorf wollte ihm dabei zusehen"
        self.assertGreater(len(vorn), MAX_SATZ_ZEICHEN)
        self.assertEqual([vorn, hinten], split_sentences(f"{vorn} {hinten}"))

    def test_10f_monster_ohne_interpunktion_teilt_an_der_harten_grenze(self):
        from mimic.voices import MAX_SATZ_HART, split_sentences
        text = " ".join(["wort"] * 120)
        teile = split_sentences(text)
        self.assertGreater(len(teile), 1)
        for teil in teile:
            self.assertLessEqual(len(teil), MAX_SATZ_HART)
        self.assertEqual(sorted(text.split()), sorted(" ".join(teile).split()))

    def test_10c_hart_umgebrochener_absatz_bleibt_ein_einsatz(self):
        from mimic.gui import Einsatz, parse_skript
        quelle = ("#matthias: Dieser Satz beginnt in der ersten Zeile,\n"
                  "wird in der zweiten Zeile fortgesetzt und\n"
                  "endet erst in der dritten Zeile.\n")
        self.assertEqual([
            Einsatz("matthias", ("Dieser Satz beginnt in der ersten Zeile, wird in der "
                                 "zweiten Zeile fortgesetzt und endet erst in der dritten Zeile.")),
        ], parse_skript(quelle, "standard"))

    def test_11_verstaerkung_hebt_referenz_auf_zielpegel(self):
        import math
        import wave
        from mimic import voices
        pfad = Path(self.enterContext(tempfile.TemporaryDirectory())) / "leise.wav"
        rate, dauer = 48_000, 1.0
        leise = [int(3000 * math.sin(i * 0.05)) for i in range(int(rate * dauer))]
        with wave.open(str(pfad), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
            wav.writeframes(array.array("h", leise).tobytes())
        gain = voices._reference_gain(str(pfad))
        ist = math.sqrt(sum(float(v) * v for v in leise) / len(leise)) / 32768
        self.assertAlmostEqual(20 * math.log10(ist * gain), voices.ZIEL_RMS_DBFS, places=1)
        self.assertLessEqual(gain, voices.MAX_GAIN)

    def test_12_stille_referenz_erzeugt_keine_verstaerkung(self):
        import wave
        from mimic import voices
        pfad = Path(self.enterContext(tempfile.TemporaryDirectory())) / "stille.wav"
        with wave.open(str(pfad), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48_000)
            wav.writeframes(bytes(96_000))
        # Ohne Deckel wuerde eine stumme Referenz das Rauschen hochziehen.
        self.assertEqual(1.0, voices._reference_gain(str(pfad)))

    def test_13_record_schreibt_ladbares_profil(self):
        # Aufnahme selbst braucht Hardware; geprueft wird der Schreibpfad, denn
        # dort entstehen die Rechte, an denen load_voice sonst scheitert.
        import math
        from mimic import cli
        from mimic.charaktere import CHARAKTERE
        from mimic.voices import close_voice, load_voice
        arbeit = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = arbeit / "voices"
        aufnahme = arbeit / "aufnahme.wav"
        rate = 48_000
        with wave.open(str(aufnahme), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
            wav.writeframes(array.array(
                "h", [int(9000 * math.sin(i * 0.05)) for i in range(rate * 5)]).tobytes())
        text = CHARAKTERE["matthias_krieger"].text
        cli.speichern(root / "matthias_krieger", aufnahme, text)
        self.assertEqual(0o700, os.stat(root / "matthias_krieger").st_mode & 0o777)
        self.assertEqual(0o600, os.stat(root / "matthias_krieger" / "ref.wav").st_mode & 0o777)
        profil = load_voice("matthias_krieger", root)
        self.assertEqual(" ".join(text.split()), profil.prompt_text)
        close_voice(profil)

    def test_10g_pause_nur_am_satzende(self):
        # Lange Kommasaetze werden an Klauselgrenzen zerteilt (MAX_SATZ_ZEICHEN).
        # Diese Schnitte sind Generierungsgrenzen, keine Sprechpausen -- der
        # Worker haengt die Atempause deshalb nur an echte Satzenden.
        from mimic.voices import endet_satz, split_sentences
        text = ("Er ging zum Hafen, der schon lange keiner mehr war, und blieb dort stehen, "
                "bis es dunkel wurde und der Regen einsetzte. Dann kehrte er endlich um.")
        teile = split_sentences(text)
        self.assertGreater(len(teile), 2)                   # an Kommas zerteilt
        pausen = [i for i in range(1, len(teile)) if endet_satz(teile[i - 1])]
        self.assertEqual([teile[i].startswith("Dann") for i in pausen], [True])
        self.assertTrue(endet_satz('Er sagte: "Genug."'))   # schliessendes Zeichen zaehlt mit
        self.assertFalse(endet_satz("und blieb dort stehen,"))

    def test_10h_versalien_werden_entschaerft(self):
        # Gemessen mit n0rd0m: "ANSWER:" kam als "Anna's door" heraus,
        # "Answer:" sauber. Akronyme bleiben, die will man buchstabiert hoeren.
        from mimic.voices import entschaerfe_versalien
        self.assertEqual("Answer: Nordom weiss es nicht.",
                         entschaerfe_versalien("ANSWER: Nordom weiss es nicht."))
        self.assertEqual("Statement und Beobachtung",
                         entschaerfe_versalien("STATEMENT und BEOBACHTUNG"))
        self.assertEqual("Die GPU hat 32 GB VRAM.",
                         entschaerfe_versalien("Die GPU hat 32 GB VRAM."))
        self.assertEqual("MoDus", entschaerfe_versalien("MoDus"))   # nur reine Versalien

    def test_13a_import_wandelt_fremdformat_in_ladbares_profil(self):
        # Der Import lebt von der ffmpeg-Wandlung: Stereo/44.1 kHz rein,
        # 48-kHz-Mono raus -- alles andere lehnt load_voice ab.
        import math
        from mimic import cli
        from mimic.voices import close_voice, load_voice
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg fehlt")
        arbeit = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = arbeit / "voices"
        quelle = arbeit / "quelle.wav"
        rate = 44_100
        rahmen = array.array("h")
        for i in range(rate * 10):
            wert = int(9000 * math.sin(i * 0.05))
            rahmen.extend((wert, wert))          # Stereo, damit -ac 1 etwas zu tun hat
        with wave.open(str(quelle), "wb") as wav:
            wav.setnchannels(2); wav.setsampwidth(2); wav.setframerate(rate)
            wav.writeframes(rahmen.tobytes())
        args = argparse.Namespace(voice="importprobe", datei=str(quelle),
                                  text="Ein Satz. Und noch einer?", force=False)
        with unittest.mock.patch.object(cli, "default_voices_dir", lambda: root):
            self.assertEqual(0, cli.importieren(args))
            profil = load_voice("importprobe", root)
            with wave.open(profil.wav_path, "rb") as wav:
                self.assertEqual((1, 48_000), (wav.getnchannels(), wav.getframerate()))
            self.assertEqual("Ein Satz. Und noch einer?", profil.prompt_text)
            close_voice(profil)
            # Zweiter Lauf ohne --force darf das Profil nicht anfassen.
            self.assertEqual(1, cli.importieren(args))

    def test_14_stumme_takes_werden_von_gesprochenen_getrennt(self):
        # Die Schwelle entscheidet, ob ein Satz nochmal erzeugt wird. Zu hoch
        # und jeder Satz kommt doppelt, zu tief und der stumme Take bleibt.
        # Die Werte sind gemessene Spitzen aus je einem echten Lauf.
        from mimic.worker import STUMM_PEAK, peak_int16
        def block(dbfs):
            spitze = int(32768 * 10 ** (dbfs / 20))
            return array.array("h", [spitze, -spitze, 0, 0]).tobytes()
        for dbfs in (-6.4, -12.1):      # gesprochen
            self.assertGreaterEqual(peak_int16(block(dbfs)), STUMM_PEAK, f"{dbfs} dBFS")
        for dbfs in (-31.5, -35.1):     # stumm
            self.assertLess(peak_int16(block(dbfs)), STUMM_PEAK, f"{dbfs} dBFS")
        self.assertEqual(0, peak_int16(b""))

    def test_15_skript_zerlegung(self):
        from mimic.gui import Einsatz, parse_skript
        quelle = ('// Notiz\n'
                  '#matthias_krieger: "Der Turm steht offen."\n'
                  '\n'
                  'Weiter spricht der Krieger.\n'
                  '#matthias_magier: Weisst du, was das bedeutet?\n')
        self.assertEqual([
            Einsatz("matthias_krieger", "Der Turm steht offen."),
            Einsatz("matthias_krieger", "Weiter spricht der Krieger."),
            Einsatz("matthias_magier", "Weisst du, was das bedeutet?"),
        ], parse_skript(quelle, "matthias"))
        # Ohne Kopf gilt die ausgewaehlte Stimme, und ein Doppelpunkt im Text
        # darf keinen Sprecherwechsel ausloesen.
        self.assertEqual([Einsatz("matthias", "Er sagte: komm herein.")],
                         parse_skript("Er sagte: komm herein.", "matthias"))
        self.assertEqual([], parse_skript("\n// nur Kommentar\n", "matthias"))


class StimmEinstellungenTests(unittest.TestCase):
    """`settings.json` im Profil: Sprach-Tag und speaker_scale je Stimme.

    Beides gehoert an die Stimme und nicht an die Anfrage: eine englische
    Referenz braucht bei deutschem Text einen niedrigeren `speaker_scale`,
    und das kann kein Aufrufer wissen. Gemessen am 2026-08-09 gegen die
    n0rd0m-Referenz -- 1.5 war britisch bis zur Unverstaendlichkeit, 0.8 traegt.
    """

    def einstellungen(self, root: Path, name: str, inhalt: str) -> None:
        pfad = root / name / "settings.json"
        pfad.write_text(inhalt, encoding="utf-8")
        pfad.chmod(0o600)

    def test_ohne_datei_gelten_die_vorgaben(self):
        from mimic.voices import DEFAULT_SCALE, DEFAULT_SPRACHE, close_voice, load_voice
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "voices"
        create_voice(root)
        profil = load_voice("matthias", root)
        self.assertEqual(DEFAULT_SPRACHE, profil.language)
        self.assertEqual(DEFAULT_SCALE, profil.speaker_scale)
        close_voice(profil)

    def test_werte_aus_der_datei_gewinnen(self):
        from mimic.voices import close_voice, load_voice
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "voices"
        create_voice(root, "n0rd0m")
        self.einstellungen(root, "n0rd0m", '{"language": "de", "speaker_scale": 0.8}')
        profil = load_voice("n0rd0m", root)
        self.assertEqual("de", profil.language)
        self.assertEqual(0.8, profil.speaker_scale)
        close_voice(profil)

    def test_unbrauchbare_werte_werden_abgewiesen(self):
        from mimic.voices import VoiceError, load_voice
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "voices"
        create_voice(root)
        # Ein stiller Rueckfall auf die Vorgabe waere schlimmer als der Fehler:
        # die Stimme klaenge falsch und niemand wuesste warum.
        for inhalt in ('{"language": "klingonisch"}', '{"speaker_scale": 0}',
                       '{"speaker_scale": 99}', '{"speaker_scale": "laut"}',
                       'kein json', '[]'):
            with self.subTest(inhalt=inhalt):
                self.einstellungen(root, "matthias", inhalt)
                with self.assertRaises(VoiceError) as fall:
                    load_voice("matthias", root)
                self.assertEqual("invalid_voice_profile", fall.exception.reason)

    def test_falsche_rechte_werden_abgewiesen(self):
        from mimic.voices import VoiceError, load_voice
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "voices"
        create_voice(root)
        self.einstellungen(root, "matthias", '{"language": "de"}')
        (root / "matthias" / "settings.json").chmod(0o644)
        with self.assertRaises(VoiceError):
            load_voice("matthias", root)


class SetupTests(unittest.TestCase):
    """install_units ist der einzige Teil von `mimic setup`, der ohne systemd laeuft."""

    def test_neu_ersetzt_unveraendert(self):
        from mimic.cli import UNITS, install_units, unit_quelle

        quelle = unit_quelle()
        assert quelle is not None, "systemd/ muss im Repo liegen"
        ziel = Path(self.enterContext(tempfile.TemporaryDirectory())) / "user"

        self.assertEqual({"neu"}, {zustand for _, zustand in install_units(quelle, ziel)})
        self.assertEqual({"unveraendert"}, {zustand for _, zustand in install_units(quelle, ziel)})
        for name in UNITS:
            self.assertEqual((quelle / name).read_bytes(), (ziel / name).read_bytes())
            self.assertEqual(0o644, (ziel / name).stat().st_mode & 0o777)

        (ziel / UNITS[0]).write_bytes(b"veraltet\n")
        zustaende = dict(install_units(quelle, ziel))
        self.assertEqual("ersetzt", zustaende[UNITS[0]])
        self.assertEqual("unveraendert", zustaende[UNITS[1]])


class LeerlaufTests(unittest.TestCase):
    """Der Leerlauf-Exit darf nur greifen, wenn wirklich niemand verbunden hat."""

    def test_verbindung_ist_kein_leerlauf(self):
        import socketserver

        from mimic.frontend import _server
        from mimic.worker import leerlauf_wache

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(16)
                self.request.sendall(b"da\n")

        pfad = Path(self.enterContext(tempfile.TemporaryDirectory())) / "s.socket"
        server = _server(Handler, pfad)
        self.addCleanup(server.server_close)
        server.timeout = 0.1
        leerlauf = leerlauf_wache(server)

        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(pfad))
            server.handle_request()
            # Kehrt sofort zurueck, weil der Handler in einem eigenen Thread laeuft.
            # Genau hier hielt die alte Zeitmessung eine spaete Anfrage fuer Leerlauf.
            self.assertFalse(leerlauf.is_set())
            client.sendall(b"hallo")
            self.assertEqual(b"da\n", client.recv(16))

        server.handle_request()
        self.assertTrue(leerlauf.is_set())
