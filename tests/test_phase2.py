from __future__ import annotations

import array
import heapq
import io
import itertools
import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class FakeHub:
    def __init__(self, path: Path, answers: list[bytes]):
        self.path = path
        self.answers = iter(answers)
        self.requests: list[dict] = []
        self.connections = 0
        self.done = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            for answer in self.answers:
                conn, _ = self.server.accept()
                self.connections += 1
                with conn:
                    raw = conn.makefile("rb").readline(4097)
                    self.requests.append(json.loads(raw))
                    if answer:
                        conn.sendall(answer)
        finally:
            self.done.set()

    def close(self):
        self.server.close()
        self.thread.join(1)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def pcm(value: int, count: int = 8) -> bytes:
    return array.array("h", [value] * count).tobytes()


class StubRuntime:
    sample_rate = 48_000

    def __init__(self, takes: list[list[bytes]]):
        self.takes = iter(takes)
        self.texts: list[str] = []

    def generate_stream(self, *, text: str, **_kwargs):
        self.texts.append(text)
        chunks = next(self.takes)
        return (chunk for chunk in chunks)


def run_engine(runtime: StubRuntime, request: dict):
    from mimic import worker

    engine = worker.Engine.__new__(worker.Engine)
    engine.runtimes = {"mf": runtime}
    engine.state = "warm"
    engine.state_lock = threading.Lock()
    engine.mode = "mf"
    engine.last_load_s = 0.0
    engine.started = time.monotonic()
    engine.fatal = threading.Event()
    events = []
    engine.emit = lambda _job, kind, payload: events.append((kind, payload)) or True
    engine.write_status = lambda *_args: None
    job = worker.Job(0, 0, {"text": "Der Anfang bleibt erhalten.", "voice": "matthias",
                            "mode": "mf", "aussprache": True,
                            "correlation_id": "1" * 32, **request})
    job.delivered.set()
    profile = SimpleNamespace(wav_path="stub.wav", prompt_text="stub", gain=1.0)
    with (mock.patch.object(worker, "load_voice", return_value=profile),
          mock.patch.object(worker, "close_voice"),
          mock.patch.object(worker, "tensor_to_pcm", side_effect=lambda chunk, _gain: chunk),
          mock.patch.object(worker, "peak_vram_mib", return_value=0),
          mock.patch.object(worker, "wake_listener")):
        engine._execute(job)
    return engine, events


def fresh_engine():
    from mimic import worker

    engine = worker.Engine.__new__(worker.Engine)
    engine.jobs = []
    engine.sequence = itertools.count()
    engine.runtimes = {}
    engine.state = "cold"
    engine.state_lock = threading.Lock()
    engine.condition = threading.Condition(engine.state_lock)
    engine.warm_request = None
    engine.warming_mode = None
    engine.loading_mode = None
    engine.mode = None
    engine.last_load_s = None
    engine.vram_free_mib = None
    engine.started = time.monotonic()
    engine.fatal = threading.Event()
    engine.write_status = lambda *_args: None
    return engine


class HubProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "daimon").mkdir()
        self.saved_env = {key: os.environ.get(key) for key in
                          ("XDG_RUNTIME_DIR", "MIMIC_VRAM_FREE_MIB",
                           "MIMIC_FAKE_LOAD_MARKER")}
        os.environ["XDG_RUNTIME_DIR"] = str(self.root)

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_hub_absagen_bleiben_maschinenlesbar(self):
        from mimic import worker

        for reason in ("vram", "fullscreen", "lade_sperre"):
            with self.subTest(reason=reason):
                marker = self.root / f"load-{reason}"
                os.environ["MIMIC_VRAM_FREE_MIB"] = "30000"
                os.environ["MIMIC_FAKE_LOAD_MARKER"] = str(marker)
                hub = FakeHub(self.root / "daimon/gpu.sock", [
                    json.dumps({"v": 1, "ok": False, "grund": reason}).encode() + b"\n"
                ])
                try:
                    with self.assertRaises(worker.WorkerRefusal) as raised:
                        worker.Engine.__new__(worker.Engine)._load("mf")
                    self.assertEqual("load_denied", raised.exception.reason)
                    self.assertEqual(reason, raised.exception.details["hub_reason"])
                    self.assertFalse(marker.exists())
                    self.assertEqual("laden", hub.requests[0]["art"])
                    self.assertEqual(worker.MODEL_VRAM_MIB, hub.requests[0]["vram_mib"])
                finally:
                    hub.close()
                    os.environ.pop("MIMIC_VRAM_FREE_MIB", None)
                    os.environ.pop("MIMIC_FAKE_LOAD_MARKER", None)

    def test_hub_nicht_erreichbar_faellt_offen(self):
        from mimic import worker

        release = worker.request_gpu_permission()
        release()

    def test_kaputte_hub_antworten_fallen_geschlossen(self):
        from mimic import worker

        cases = ((b"", "hub_empty"), (b"kein json\n", "hub_invalid_json"),
                 (b'{"v":1,"ok":true}\n', "hub_invalid_schema"))
        for answer, reason in cases:
            with self.subTest(reason=reason):
                marker = self.root / f"load-{reason}"
                os.environ["MIMIC_VRAM_FREE_MIB"] = "30000"
                os.environ["MIMIC_FAKE_LOAD_MARKER"] = str(marker)
                hub = FakeHub(self.root / "daimon/gpu.sock", [answer])
                try:
                    with self.assertRaises(worker.WorkerRefusal) as raised:
                        worker.Engine.__new__(worker.Engine)._load("mf")
                    self.assertEqual(reason, raised.exception.details["hub_reason"])
                    self.assertFalse(marker.exists())
                finally:
                    hub.close()
                    os.environ.pop("MIMIC_VRAM_FREE_MIB", None)
                    os.environ.pop("MIMIC_FAKE_LOAD_MARKER", None)

    def test_fertig_nutzt_neue_verbindung_und_gibt_sperre_frei(self):
        from mimic import worker

        token1, token2 = "a" * 32, "b" * 32
        hub = FakeHub(self.root / "daimon/gpu.sock", [
            json.dumps({"v": 1, "ok": True, "sperre": token1}).encode() + b"\n",
            b'{"v":1,"ok":true}\n',
            json.dumps({"v": 1, "ok": True, "sperre": token2}).encode() + b"\n",
        ])
        try:
            worker.request_gpu_permission()()
            worker.request_gpu_permission()
            self.assertTrue(hub.done.wait(1))
            self.assertEqual(3, hub.connections)
            self.assertEqual(["laden", "fertig", "laden"],
                             [request["art"] for request in hub.requests])
            self.assertEqual(token1, hub.requests[1]["sperre"])
        finally:
            hub.close()


class WorkerDefectTests(unittest.TestCase):
    def test_aussprache_false_belaesst_den_hub_text(self):
        from mimic import worker

        runtime = StubRuntime([[pcm(worker.STUMM_PEAK + 1)]])
        with mock.patch.object(worker, "apply_pronunciation",
                               side_effect=AssertionError("Tabelle darf nicht laufen")):
            _engine, events = run_engine(runtime, {"aussprache": False})
        self.assertEqual(["Der Anfang bleibt erhalten."], runtime.texts)
        self.assertEqual("ok", json.loads(events[-1][1])["status"])

    def test_zeichenfilter_blockiert_pfade_und_urls(self):
        from mimic.voices import apply_pronunciation

        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "pronunciation.json"
        path.write_text(json.dumps({"gut": "/etc/passwd", "web": "https://example.test",
                                    "harmlos": "anderer Satz", "main": "mayn"}),
                        encoding="utf-8")
        self.assertEqual("gut web anderer Satz mayn",
                         apply_pronunciation("gut web harmlos main", path))

    def test_stummer_take_wird_verworfen_bis_hoerbarer_take_beginnt(self):
        from mimic import worker

        quiet = pcm(worker.STUMM_PEAK)
        loud = pcm(worker.STUMM_PEAK + 1)
        started = time.monotonic()
        _engine, events = run_engine(StubRuntime([[quiet], [quiet, loud]]), {})
        self.assertLess(time.monotonic() - started, 0.2)
        audio = [payload for kind, payload in events if kind == "A"]
        self.assertEqual(1, len(audio))
        self.assertEqual(quiet + loud, audio[0])
        self.assertGreater(worker.peak_int16(audio[0]), worker.STUMM_PEAK)

    def test_zwei_stumme_takes_senden_keinen_audio_rahmen(self):
        from mimic import worker

        quiet = pcm(worker.STUMM_PEAK)
        _engine, events = run_engine(StubRuntime([[quiet], [quiet]]), {})
        self.assertEqual(["H", "E"], [kind for kind, _ in events])
        end = json.loads(events[-1][1])
        self.assertEqual("silent_audio", end["reason"])

    def test_moduswechsel_beendet_worker_ohne_runtime_zu_raeumen(self):
        from mimic import worker

        old_runtime = StubRuntime([])
        engine, events = run_engine(old_runtime, {"mode": "soar"})
        self.assertIs(old_runtime, engine.runtimes["mf"])
        self.assertTrue(engine.fatal.is_set())
        end = json.loads(events[-1][1])
        self.assertEqual("mode_restart", end["reason"])
        self.assertEqual(os.getpid(), end["worker_pid"])

        engine.state = "loading"
        engine.jobs = mock.Mock()
        engine.sequence = iter((1,))
        with self.assertRaises(worker.WorkerRefusal) as raised:
            engine.submit({"text": "x", "voice": "matthias", "mode": "mf"})
        self.assertEqual("cold", raised.exception.reason)

    def test_fehlgeschlagener_kaltstart_bleibt_cold(self):
        from mimic import worker

        engine = worker.Engine.__new__(worker.Engine)
        engine.runtimes = {}
        engine.state = "cold"
        engine.state_lock = threading.Lock()
        engine.condition = threading.Condition(engine.state_lock)
        engine.loading_mode = None
        engine.mode = None
        engine.last_load_s = None
        engine.started = time.monotonic()
        engine.fatal = threading.Event()
        engine.write_status = lambda *_args: None
        events = []
        engine.emit = lambda _job, kind, payload: events.append((kind, payload)) or True
        job = worker.Job(0, 0, {"text": "x", "voice": "matthias", "mode": "mf"})
        profile = SimpleNamespace(wav_path="stub.wav", prompt_text="stub", gain=1.0)
        with mock.patch.object(worker, "load_voice", return_value=profile), \
             mock.patch.object(worker, "close_voice"), \
             mock.patch.object(engine, "_load",
                               side_effect=worker.WorkerRefusal("load_denied", "nein",
                                                                hub_reason="vram")), \
             mock.patch.object(worker, "peak_vram_mib", return_value=0):
            engine._execute(job)
        self.assertEqual("cold", engine.state)
        self.assertEqual("load_denied", json.loads(events[-1][1])["reason"])

    def test_frontend_wiederholt_moduswechsel_genau_einmal(self):
        from mimic.frontend import FrontendHandler

        handler = FrontendHandler.__new__(FrontendHandler)
        calls = []
        handler._proxy_once = lambda request, retry_mode: calls.append(retry_mode) or retry_mode
        handler._error = lambda *_args, **_kwargs: self.fail("unerwarteter Fehler")
        handler._proxy({"mode": "soar"})
        self.assertEqual([True, False], calls)

    def test_frontend_reicht_hub_reason_nach_aussen(self):
        from mimic import frontend

        payload = json.dumps({"status": "error", "reason": "load_denied",
                              "message": "abgelehnt", "hub_reason": "fullscreen"}).encode()

        class Response:
            status = 200

            def close(self):
                pass

        class Connection:
            sock = SimpleNamespace(settimeout=lambda _timeout: None)

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        handler = frontend.FrontendHandler.__new__(frontend.FrontendHandler)
        result = {}
        handler._json = lambda status, value: result.update(status=status, value=value)
        with mock.patch.object(frontend, "UnixHTTPConnection", return_value=Connection()), \
             mock.patch.object(frontend, "_set_response_timeout"), \
             mock.patch.object(frontend, "read_frame", return_value=("E", payload)):
            retry = handler._proxy_once({"text": "x"}, retry_mode=False)
        self.assertFalse(retry)
        self.assertEqual(503, result["status"])
        self.assertEqual("load_denied", result["value"]["reason"])
        self.assertEqual("fullscreen", result["value"]["hub_reason"])


class SchemaAndGuiTests(unittest.TestCase):
    def _frontend_request(self, value: dict):
        from mimic import frontend

        body = json.dumps(value).encode()
        handler = frontend.FrontendHandler.__new__(frontend.FrontendHandler)
        handler.path = "/speak"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        result = {}
        handler._proxy = lambda request: result.setdefault("request", request)
        handler._error = lambda reason, message, **details: result.setdefault(
            "error", (reason, message, details))
        profile = SimpleNamespace(wav_path="/proc/self/fd/-1")
        with mock.patch.object(frontend, "load_voice", return_value=profile), \
             mock.patch.object(frontend, "close_voice"):
            handler.do_POST()
        return result

    def test_altes_cli_gui_schema_behaelt_aussprache_und_erzeugt_id(self):
        result = self._frontend_request({"text": "Hallo", "voice": "matthias", "mode": "mf"})
        request = result["request"]
        self.assertIs(True, request["aussprache"])
        self.assertIs(False, request["require_warm"])
        self.assertRegex(request["correlation_id"], r"^[0-9a-f]{32}$")

        supplied = "A" * 32
        result = self._frontend_request({"text": "Hallo", "aussprache": False,
                                         "correlation_id": supplied})
        self.assertIs(False, result["request"]["aussprache"])
        self.assertEqual(supplied, result["request"]["correlation_id"])

        result = self._frontend_request({"text": "Hallo", "require_warm": True})
        self.assertIs(True, result["request"]["require_warm"])

        for invalid in ("", "g" * 32, "a" * 31, "a" * 33, "a" * 31 + "\n"):
            with self.subTest(invalid=invalid):
                result = self._frontend_request({"text": "Hallo", "correlation_id": invalid})
                self.assertEqual("bad_request", result["error"][0])

    def test_gui_stopp_schliesst_vor_kopf_waehrend_stream_und_vor_start(self):
        from mimic import gui

        stopped = threading.Event()
        active = gui.AktiveVerbindung(stopped)
        stopped.set()
        with mock.patch.object(gui, "open_request") as opened:
            with self.assertRaises(gui.Abgebrochen):
                active.anfrage({"text": "x"})
            opened.assert_not_called()

        stopped.clear()
        client, peer = socket.socketpair()
        entered = threading.Event()

        class BlockingConnection:
            sock = client

            def getresponse(self):
                entered.set()
                client.recv(1)
                raise OSError("geschlossen")

            def close(self):
                client.close()

        result = []
        connection = BlockingConnection()
        def open_blocking(*_args, publish, **_kwargs):
            publish(connection)
            return connection
        with mock.patch.object(gui, "open_request", side_effect=open_blocking):
            thread = threading.Thread(target=lambda: result.append(self._blocked(active)))
            thread.start()
            self.assertTrue(entered.wait(0.5))
            active.abbrechen()
            thread.join(0.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(["abgebrochen"], result)
        peer.close()

        stopped.clear()
        client, peer = socket.socketpair()
        response = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=client)),
                                   close=client.close)
        active.conn = SimpleNamespace(sock=None, close=lambda: None)
        active.response = response
        active.abbrechen()
        peer.settimeout(0.2)
        self.assertEqual(b"", peer.recv(1))
        peer.close()

    @staticmethod
    def _blocked(active):
        try:
            active.anfrage({"text": "x"})
        except Exception as exc:
            from mimic.gui import Abgebrochen
            return "abgebrochen" if isinstance(exc, Abgebrochen) else type(exc).__name__
        return "offen"


class OwnerFreigabeTests(unittest.TestCase):
    """Der Owner-Thread ist einer fuer alle Jobs. Haengt er, ist der Dienst tot."""

    def test_wegbrechender_leser_vor_dem_kopf_gibt_den_owner_frei(self):
        from mimic import worker

        engine = fresh_engine()
        engine.state = "warm"
        engine.runtimes = {"mf": object()}
        koerper = json.dumps({"text": "x", "voice": "matthias", "mode": "mf"}).encode()

        class ToterSocket(io.BytesIO):
            def write(self, _daten):
                raise BrokenPipeError(32, "Broken pipe")

        handler = worker.WorkerHandler.__new__(worker.WorkerHandler)
        handler.path = "/synthesize"
        handler.request_version = "HTTP/1.1"
        handler.headers = {"Content-Length": str(len(koerper))}
        handler.rfile = io.BytesIO(koerper)
        handler.wfile = ToterSocket()
        handler.log_request = lambda *_args, **_kwargs: None
        # ENGINE ist im Modul nur annotiert, den Wert setzt erst main().
        with mock.patch.object(worker, "ENGINE", engine, create=True):
            handler.do_POST()

        self.assertEqual(1, len(engine.jobs), "Job wurde eingereiht")
        job = engine.jobs[0]
        self.assertTrue(job.cancelled.is_set(),
                        "cancelled muss gesetzt sein, sonst haengt emit() ewig")
        self.assertFalse(engine.emit(job, "A", b"\0\0"))

    def test_emit_gibt_nach_der_frist_auf_statt_ewig_zu_warten(self):
        from mimic import worker

        engine = fresh_engine()
        job = worker.Job(0, 0, {"text": "x", "voice": "matthias", "mode": "mf"})
        while not job.events.full():
            job.events.put(("A", b"\0\0"))
        with mock.patch.object(worker, "REQUEST_TIMEOUT", 0.2):
            gestartet = time.monotonic()
            self.assertFalse(engine.emit(job, "A", b"\0\0"))
        self.assertLess(time.monotonic() - gestartet, 2.0)
        self.assertTrue(job.cancelled.is_set())


class Phase2bTests(unittest.TestCase):
    def test_require_warm_wird_vor_dem_einreihen_abgelehnt(self):
        from mimic import worker

        engine = fresh_engine()
        with self.assertRaises(worker.WorkerRefusal) as raised:
            engine.submit({"text": "x", "voice": "matthias", "mode": "mf",
                           "require_warm": True})
        self.assertEqual("cold", raised.exception.reason)
        self.assertEqual([], engine.jobs)

        engine.state = "warm"
        engine.runtimes = {"soar": object()}
        with self.assertRaises(worker.WorkerRefusal):
            engine.submit({"text": "x", "voice": "matthias", "mode": "mf",
                           "require_warm": True})
        engine.runtimes["mf"] = object()
        job = engine.submit({"text": "x", "voice": "matthias", "mode": "mf",
                             "require_warm": True})
        self.assertIs(job, engine.jobs[0])

    def test_require_warm_mitten_im_warmlauf_wartet_nicht_auf_load(self):
        from mimic import worker

        engine = fresh_engine()
        entered = threading.Event()
        release = threading.Event()

        def slow_load(_mode):
            entered.set()
            self.assertTrue(release.wait(1))
            return object()

        engine._load = slow_load
        thread = threading.Thread(target=engine._warm,
                                  args=({"mode": "mf", "correlation_id": "1" * 32},))
        thread.start()
        self.assertTrue(entered.wait(0.5))
        started = time.monotonic()
        with self.assertRaises(worker.WorkerRefusal) as raised:
            engine.submit({"text": "x", "voice": "matthias", "mode": "mf",
                           "require_warm": True})
        self.assertEqual("cold", raised.exception.reason)
        self.assertLess(time.monotonic() - started, 0.1)
        release.set()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())

    def test_initialstatus_fasst_vram_und_torch_nicht_an(self):
        from mimic import worker

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        old_runtime = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = str(root)
        self.addCleanup(lambda: (os.environ.pop("XDG_RUNTIME_DIR", None) if old_runtime is None
                                 else os.environ.__setitem__("XDG_RUNTIME_DIR", old_runtime)))
        idle_thread = SimpleNamespace(start=lambda: None)
        with mock.patch.object(worker.threading, "Thread", return_value=idle_thread), \
             mock.patch.object(worker, "vram_free_mib",
                               side_effect=AssertionError("VRAM-Abfrage zu frueh")):
            worker.Engine()
        value = json.loads((root / "mimic/worker-status.json").read_text())
        self.assertEqual(1, value["v"])
        self.assertIsNone(value["vram_free_mib"])

    def test_warmwunsch_weckt_condition_und_belegt_keinen_jobplatz(self):
        from mimic import worker

        engine = fresh_engine()
        seen = threading.Event()

        def consume(request):
            seen.set()
            raise SystemExit

        engine._warm = consume
        owner = threading.Thread(target=engine._run, daemon=True)
        owner.start()
        self.assertEqual(202, engine.request_warm(
            {"mode": "mf", "correlation_id": "2" * 32}))
        self.assertTrue(seen.wait(0.5))
        owner.join(0.5)

        engine = fresh_engine()
        self.assertEqual(202, engine.request_warm({"mode": "mf"}))
        self.assertEqual(409, engine.request_warm({"mode": "mf"}))
        soar = engine.submit({"text": "x", "voice": "matthias", "mode": "soar"})
        mf = engine.submit({"text": "x", "voice": "matthias", "mode": "mf"})
        for _ in range(2):
            engine.submit({"text": "x", "voice": "matthias", "mode": "soar"})
        with self.assertRaises(worker.WorkerRefusal) as raised:
            engine.submit({"text": "x", "voice": "matthias", "mode": "mf"})
        self.assertEqual("busy", raised.exception.reason)
        self.assertIs(mf, heapq.heappop(engine.jobs))
        self.assertIs(soar, heapq.heappop(engine.jobs))
        self.assertIsNotNone(engine.warm_request)

        engine.warm_request = None
        engine.state = "warm"
        engine.runtimes = {"mf": object()}
        self.assertEqual(200, engine.request_warm({"mode": "mf"}))
        with self.assertRaises(worker.WorkerRefusal) as raised:
            engine.request_warm({"mode": "soar"})
        self.assertEqual("bad_request", raised.exception.reason)

    def test_status_listet_nur_ladbare_stimmen_mit_version(self):
        from mimic import frontend
        from tests.test_phase1 import create_voice

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        voices = root / "voices"
        create_voice(voices, "valid")
        broken = voices / "broken"
        broken.mkdir(mode=0o700)
        (broken / "ref.txt").write_text("unvollstaendig", encoding="utf-8")
        (broken / "ref.txt").chmod(0o600)
        saved = {key: os.environ.get(key) for key in ("MIMIC_VOICES_DIR", "XDG_RUNTIME_DIR")}
        os.environ["MIMIC_VOICES_DIR"] = str(voices)
        os.environ["XDG_RUNTIME_DIR"] = str(root)
        self.addCleanup(self._restore_env, saved)
        handler = frontend.FrontendHandler.__new__(frontend.FrontendHandler)
        result = {}
        handler.path = "/status"
        handler._json = lambda status, value: result.update(status=status, value=value)
        handler.do_GET()
        self.assertEqual(200, result["status"])
        self.assertEqual(1, result["value"]["v"])
        self.assertEqual(["valid"], result["value"]["voices"])

    def test_fehler_bleiben_versioniert_strukturiert_und_unbekannt(self):
        from mimic import frontend

        handler = frontend.FrontendHandler.__new__(frontend.FrontendHandler)
        result = {}
        handler._json = lambda status, value: result.update(status=status, value=value)
        handler._error("future_reason", "neu", hub_reason="vram", secret="weg")
        self.assertEqual(503, result["status"])
        self.assertEqual({"v": 1, "reason": "future_reason", "message": "neu",
                          "hub_reason": "vram"}, result["value"])
        handler._error("cold", "kalt")
        self.assertEqual(503, result["status"])
        self.assertEqual("cold", result["value"]["reason"])

        class FakeReader:
            def __init__(self, _body):
                self.events = queue.Queue()
                self.events.put(("response", object()))
                payload = json.dumps({"status": "error", "reason": "future_worker_reason",
                                      "message": "bleibt"}).encode()
                self.events.put(("frame", "E", payload))

            def start(self):
                pass

            def close(self):
                pass

        with mock.patch.object(frontend, "_WorkerReader", FakeReader):
            handler._proxy_once({"text": "x"}, retry_mode=False)
        self.assertEqual("future_worker_reason", result["value"]["reason"])

    def test_frontend_warm_reicht_202_200_409_durch_und_lehnt_soar_ab(self):
        from mimic import frontend

        class Response:
            def __init__(self, status):
                self.status = status

            def read(self, _limit):
                return json.dumps({"v": 1, "status": self.status}).encode()

            def close(self):
                pass

        for status in (202, 200, 409):
            with self.subTest(status=status):
                sent = {}

                class Connection:
                    sock = SimpleNamespace(settimeout=lambda _value: None)

                    def request(self, method, path, body, _headers):
                        sent.update(method=method, path=path, body=json.loads(body))

                    def getresponse(self):
                        return Response(status)

                    def close(self):
                        pass

                handler = frontend.FrontendHandler.__new__(frontend.FrontendHandler)
                result = {}
                handler._json = lambda code, value: result.update(status=code, value=value)
                with mock.patch.object(frontend, "UnixHTTPConnection", return_value=Connection()):
                    handler._handle_warm({"mode": "mf", "correlation_id": "a" * 32})
                self.assertEqual(status, result["status"])
                self.assertEqual("/warm", sent["path"])
                self.assertEqual("mf", sent["body"]["mode"])

        handler = frontend.FrontendHandler.__new__(frontend.FrontendHandler)
        result = {}
        handler._error = lambda reason, message, **details: result.update(reason=reason)
        handler._handle_warm({"mode": "soar"})
        self.assertEqual("bad_request", result["reason"])

    @staticmethod
    def _restore_env(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
