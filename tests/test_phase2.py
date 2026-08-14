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
        self.kwargs: list[dict] = []

    def generate_stream(self, *, text: str, **kwargs):
        self.texts.append(text)
        self.kwargs.append(kwargs)
        chunks = next(self.takes)
        return (chunk for chunk in chunks)


def run_engine(runtime: StubRuntime, request: dict, *, stimme: dict | None = None):
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
    profile = SimpleNamespace(wav_path="stub.wav", prompt_text="stub", gain=1.0,
                              **{"language": "en", "speaker_scale": 1.5, "effekt": "",
                                 "tonhoehe": 0.0, "streuung": 0.0, "raster": 0.0,
                                 "formant": 0.0, "hall": 0.0, "verzerrung": 0.0,
                                 "kruemel": 0.0, "breite": 0.0, **(stimme or {})})
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
                                    "harmlos": "anderer Satz", "main": "mayn",
                                    "Satzende": "Satz-Ende"}),
                        encoding="utf-8")
        self.assertEqual("gut web anderer Satz mayn Satz-Ende",
                         apply_pronunciation("gut web harmlos main Satzende", path))

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

    def test_langer_stiller_vorlauf_wird_bis_auf_einen_chunk_verworfen(self):
        # Gemessen am 2026-08-08: das Modell erzeugt stochastisch bis zu 16
        # stille Chunks (je ~154 ms) vor dem ersten Ton. Wer die mitsendet,
        # laesst den Hoerer die Stille abspielen -- bis zu einer Sekunde nach
        # dem ersten Rahmen. Ein Chunk bleibt als Luft fuer weiche Anlaute.
        from mimic import worker

        quiet = pcm(worker.STUMM_PEAK)
        loud = pcm(worker.STUMM_PEAK + 1)
        _engine, events = run_engine(StubRuntime([[quiet] * 4 + [loud]]), {})
        audio = [payload for kind, payload in events if kind == "A"]
        self.assertEqual(quiet + loud, audio[0])

    def test_durchgehend_stumme_takes_senden_keinen_audio_rahmen(self):
        from mimic import worker

        quiet = pcm(worker.STUMM_PEAK)
        _engine, events = run_engine(
            StubRuntime([[quiet]] * worker.MAX_VERSUCHE), {})
        self.assertEqual(["H", "E"], [kind for kind, _ in events])
        end = json.loads(events[-1][1])
        self.assertEqual("silent_audio", end["reason"])

    def test_stummes_stueck_beendet_die_restliche_aeusserung_nicht(self):
        # Der Fall aus dem Journal vom 2026-08-13: ein Stueck kam stumm, und
        # frueher fiel damit der ganze Rest des Textes weg.
        from mimic import worker

        quiet = pcm(worker.STUMM_PEAK)
        loud = pcm(worker.STUMM_PEAK + 1)
        takes = [[loud]] + [[quiet]] * worker.MAX_VERSUCHE + [[loud]]
        _engine, events = run_engine(
            StubRuntime(takes),
            {"text": "Der Anfang bleibt erhalten. Die Mitte kommt stumm zurueck. "
                     "Das Ende gehoert trotzdem gesprochen."})
        end = json.loads(events[-1][1])
        self.assertEqual("ok", end["status"])
        self.assertEqual(1, end["uebersprungen"])
        gesprochen = [payload for kind, payload in events
                      if kind == "A" and payload.strip(b"\x00")]
        self.assertEqual([loud, loud], gesprochen)

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
        profile = SimpleNamespace(wav_path="stub.wav", prompt_text="stub", gain=1.0,
                              language="en", speaker_scale=1.5)
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
        # soar ist nicht geladen: der Warmlauf wird angenommen und loest den
        # kontrollierten Moduswechsel aus, statt abgelehnt zu werden.
        self.assertEqual(202, engine.request_warm({"mode": "soar"}))
        with self.assertRaises(worker.WorkerRefusal) as raised:
            engine.request_warm({"mode": "quatsch"})
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

    def test_frontend_warm_reicht_202_200_409_durch_und_lehnt_fremden_modus_ab(self):
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
        handler._handle_warm({"mode": "quatsch"})
        self.assertEqual("bad_request", result["reason"])

    @staticmethod
    def _restore_env(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class AufnahmeTests(unittest.TestCase):
    """Der Aufnehmer kann beim Start sterben -- das Fenster muss es merken."""

    def _stub(self, ordner: Path, rumpf: str) -> str:
        weg = ordner / "stub-aufnehmer"
        weg.write_text("#!/bin/sh\n" + rumpf + "\n")
        weg.chmod(0o755)
        return str(weg)

    def test_gestorbener_aufnehmer_meldet_grund_statt_weiterzuzaehlen(self):
        from mimic import gui

        with tempfile.TemporaryDirectory() as ordner:
            heim = Path(ordner)
            stub = self._stub(heim, "echo 'kein Aufnahmegeraet' >&2\nexit 1")
            saved = {"MIMIC_VOICES_DIR": os.environ.get("MIMIC_VOICES_DIR")}
            os.environ["MIMIC_VOICES_DIR"] = str(heim / "voices")
            with mock.patch.object(gui, "AUFNEHMER", stub):
                try:
                    aufnahme = gui.Aufnahme()
                    aufnahme.starten("stubstimme", force=True)
                    for _ in range(200):          # auf das Ende des Stubs warten
                        stand = aufnahme.stand()
                        if not stand["laeuft"]:
                            break
                        time.sleep(0.01)
                    self.assertFalse(stand["laeuft"], "tote Aufnahme zaehlt weiter")
                    self.assertIn("kein Aufnahmegeraet", stand["abbruch"])
                    # Der abgebrochene Versuch darf kein leeres Profil hinterlassen.
                    self.assertFalse((heim / "voices" / "stubstimme").exists())
                finally:
                    self._restore_env(saved)

    def test_stopp_ohne_datei_nennt_die_meldung_des_aufnehmers(self):
        from mimic import gui

        with tempfile.TemporaryDirectory() as ordner:
            heim = Path(ordner)
            stub = self._stub(heim, "echo 'Quelle belegt' >&2\nexit 1")
            saved = {"MIMIC_VOICES_DIR": os.environ.get("MIMIC_VOICES_DIR")}
            os.environ["MIMIC_VOICES_DIR"] = str(heim / "voices")
            with mock.patch.object(gui, "AUFNEHMER", stub):
                try:
                    aufnahme = gui.Aufnahme()
                    aufnahme.starten("stubstimme", force=True)
                    aufnahme.prozess.wait(timeout=5)
                    with self.assertRaises(RuntimeError) as gefangen:
                        aufnahme.stoppen()
                    self.assertIn("Quelle belegt", str(gefangen.exception))
                finally:
                    self._restore_env(saved)

    @staticmethod
    def _restore_env(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()


class EffektTests(unittest.TestCase):
    """Der Effekt sitzt hinter tensor_to_pcm und muss ueber Bloecke tragen."""

    def test_ohne_effekt_bleibt_das_pcm_unveraendert(self):
        runtime = StubRuntime([[pcm(9000), pcm(-9000)]])
        _engine, events = run_engine(runtime, {"text": "Ein Satz, der lang genug ist."})
        audio = b"".join(payload for kind, payload in events if kind == "A")
        self.assertEqual(pcm(9000) + pcm(-9000), audio)

    def test_effekt_veraendert_das_pcm(self):
        runtime = StubRuntime([[pcm(9000), pcm(-9000)]])
        _engine, events = run_engine(runtime, {"text": "Ein Satz, der lang genug ist."},
                                     stimme={"effekt": "roboter"})
        audio = b"".join(payload for kind, payload in events if kind == "A")
        self.assertEqual(len(pcm(9000) + pcm(-9000)), len(audio))
        self.assertNotEqual(pcm(9000) + pcm(-9000), audio)

    def test_effekt_hebt_einen_stummen_take_nicht_ueber_die_schwelle(self):
        # Der Effekt lief frueher VOR der Stummerkennung, und `kollektiv` legt
        # zwei Kopien auf das Signal: ein Take knapp unter STUMM_PEAK kam damit
        # als hoerbar heraus und wurde nicht wiederholt. Seit die Kette erst in
        # `sende()` laeuft, misst `spitze` das rohe Modellsignal -- PHASE2 2a,
        # Kriterium P2-L. Bloecke gross genug, dass die Kopien (17 und 29 ms)
        # hineinfallen; bei acht Proben lesen sie noch Nullen.
        from mimic import worker

        lang = 4096
        quiet = pcm(worker.STUMM_PEAK, lang)
        loud = pcm(worker.STUMM_PEAK + 1, lang)
        from mimic.effekte import Effekt

        laut_genug = worker.peak_int16(
            Effekt("kollektiv", 48_000).verarbeite(quiet + quiet))
        self.assertGreater(laut_genug, worker.STUMM_PEAK,
                           "Testvoraussetzung: kollektiv hebt diesen Take ueber die Schwelle")

        runtime = StubRuntime([[quiet], [quiet, loud]])
        _engine, events = run_engine(runtime, {"text": "Ein Satz, der lang genug ist."},
                                     stimme={"effekt": "kollektiv"})
        self.assertEqual(2, len(runtime.texts), "der stumme Take muss wiederholt werden")
        audio = b"".join(payload for kind, payload in events if kind == "A")
        self.assertEqual(len(quiet + loud), len(audio))

    def test_jeder_name_aus_der_whitelist_laeuft_durch(self):
        from mimic.effekte import EFFEKTE

        eingang = pcm(9000, 4096)
        for name in EFFEKTE:
            with self.subTest(effekt=name):
                runtime = StubRuntime([[eingang]])
                _engine, events = run_engine(
                    runtime, {"text": "Ein Satz, der lang genug ist."},
                    stimme={"effekt": name})
                self.assertEqual("ok", json.loads(events[-1][1])["status"])
                audio = b"".join(p for kind, p in events if kind == "A")
                self.assertEqual(len(eingang), len(audio))
                self.assertNotEqual(eingang, audio)

    def test_unbekannter_effekt_wird_beim_laden_abgelehnt(self):
        from mimic.voices import VoiceError, load_voice
        import wave as wave_modul
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "voices"
        profil = root / "probe"
        profil.mkdir(mode=0o700, parents=True)
        root.chmod(0o700)
        with wave_modul.open(str(profil / "ref.wav"), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48_000)
            wav.writeframes(array.array("h", [1000] * 48_000 * 4).tobytes())
        (profil / "ref.txt").write_text("Ein Satz.\n", encoding="utf-8")
        (profil / "settings.json").write_text(json.dumps({"effekt": "rm -rf"}), encoding="utf-8")
        for datei in ("ref.wav", "ref.txt", "settings.json"):
            (profil / datei).chmod(0o600)
        with self.assertRaises(VoiceError) as erhoben:
            load_voice("probe", root)
        self.assertIn("effekt", erhoben.exception.message)

    def test_tonhoehe_und_streuung_kommen_aus_dem_profil(self):
        """Kein stiller Rueckfall: eine Stimme, die wegen eines Tippfehlers
        ploetzlich anders klingt, ist teurer zu finden als ein Ladefehler."""
        from mimic.voices import VoiceError, load_voice
        import wave as wave_modul
        root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "voices"
        profil = root / "probe"
        profil.mkdir(mode=0o700, parents=True)
        root.chmod(0o700)
        with wave_modul.open(str(profil / "ref.wav"), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48_000)
            wav.writeframes(array.array("h", [1000] * 48_000 * 4).tobytes())
        (profil / "ref.txt").write_text("Ein Satz.\n", encoding="utf-8")

        def settings(inhalt):
            (profil / "settings.json").write_text(json.dumps(inhalt), encoding="utf-8")
            for datei in ("ref.wav", "ref.txt", "settings.json"):
                (profil / datei).chmod(0o600)

        settings({"tonhoehe": -3.5, "streuung": 1, "raster": 1, "formant": 2, "hall": 0.5,
                  "verzerrung": 0.4, "kruemel": 0.2, "breite": 0.6})
        stimme = load_voice("probe", root, mit_gain=False)
        self.assertEqual((-3.5, 1.0, 1.0, 2.0, 0.5, 0.4, 0.2, 0.6),
                         (stimme.tonhoehe, stimme.streuung, stimme.raster, stimme.formant,
                          stimme.hall, stimme.verzerrung, stimme.kruemel, stimme.breite))
        for kaputt in ({"tonhoehe": 40}, {"streuung": -1}, {"tonhoehe": "tief"},
                       {"raster": 2}, {"formant": 40}, {"hall": 1.5}, {"hall": "viel"},
                       {"verzerrung": 2}, {"kruemel": -0.5}, {"breite": 3}):
            settings(kaputt)
            with self.assertRaises(VoiceError):
                load_voice("probe", root, mit_gain=False)


class TempoTests(unittest.TestCase):
    """Der Regler sitzt im Worker, hinter dem Effekt und vor dem Rahmen."""

    def sprich(self, tempo):
        eine_sekunde = pcm(9000, 48_000)
        runtime = StubRuntime([[eine_sekunde]])
        _engine, events = run_engine(runtime, {"text": "Ein Satz, der lang genug ist.",
                                               "tempo": tempo})
        self.assertEqual("ok", json.loads(events[-1][1])["status"])
        return eine_sekunde, b"".join(p for kind, p in events if kind == "A")

    def test_faktor_eins_laesst_das_pcm_in_ruhe(self):
        eingang, ausgang = self.sprich(1.0)
        self.assertEqual(eingang, ausgang)

    def test_schneller_kuerzt_die_ausgabe_um_den_faktor(self):
        eingang, ausgang = self.sprich(2.0)
        self.assertAlmostEqual(len(eingang) / 2, len(ausgang), delta=64)

    def test_langsamer_dehnt_die_ausgabe_um_den_faktor(self):
        eingang, ausgang = self.sprich(0.5)
        self.assertAlmostEqual(len(eingang) * 2, len(ausgang), delta=64)

    def test_unsinn_faellt_auf_unveraendert_zurueck(self):
        eingang, ausgang = self.sprich("schnell")
        self.assertEqual(eingang, ausgang)


class TonhoeheTests(unittest.TestCase):
    """Tonhoehe und Streuung sitzen im selben Regler wie das Tempo."""

    def sprich(self, request, stimme=None):
        eine_sekunde = pcm(9000, 48_000)
        runtime = StubRuntime([[eine_sekunde]])
        _engine, events = run_engine(runtime, {"text": "Ein Satz, der lang genug ist.",
                                               **request}, stimme=stimme)
        self.assertEqual("ok", json.loads(events[-1][1])["status"])
        return eine_sekunde, b"".join(p for kind, p in events if kind == "A")

    def test_tonhoehe_veraendert_den_ton_ohne_die_dauer(self):
        eingang, ausgang = self.sprich({"tonhoehe": 5.0})
        self.assertAlmostEqual(len(eingang), len(ausgang), delta=64)
        self.assertNotEqual(eingang, ausgang)

    def test_raster_allein_reicht_fuer_den_regler(self):
        eingang, ausgang = self.sprich({"raster": 1.0})
        self.assertAlmostEqual(len(eingang), len(ausgang), delta=64)

    def test_formant_veraendert_den_klang_ohne_die_dauer(self):
        eingang, ausgang = self.sprich({"formant": 2.0})
        self.assertAlmostEqual(len(eingang), len(ausgang), delta=64)
        self.assertNotEqual(eingang, ausgang)

    def test_glados_werte_kommen_zusammen_durch(self):
        from mimic.effekte import GLADOS

        eingang, ausgang = self.sprich(dict(GLADOS))
        self.assertAlmostEqual(len(eingang), len(ausgang), delta=64)
        self.assertNotEqual(eingang, ausgang)

    def test_profilwert_gilt_auch_ohne_regler(self):
        eingang, ausgang = self.sprich({}, stimme={"tonhoehe": -4.0})
        self.assertNotEqual(eingang, ausgang)

    def test_regler_kommt_auf_den_profilwert_obendrauf(self):
        # -4 im Profil, +4 am Regler: zusammen wieder die Ausgangslage.
        _eingang, mit_beidem = self.sprich({"tonhoehe": 4.0}, stimme={"tonhoehe": -4.0})
        _eingang, ohne_alles = self.sprich({})
        self.assertEqual(ohne_alles, mit_beidem)


class HallTests(unittest.TestCase):
    """Der Hall ist der erste Regler, der die Aeusserung laenger macht."""

    def sprich(self, request, stimme=None):
        eine_sekunde = pcm(9000, 48_000)
        runtime = StubRuntime([[eine_sekunde]])
        _engine, events = run_engine(runtime, {"text": "Ein Satz, der lang genug ist.",
                                               **request}, stimme=stimme)
        self.assertEqual("ok", json.loads(events[-1][1])["status"])
        return eine_sekunde, b"".join(p for kind, p in events if kind == "A")

    def test_hall_haengt_den_nachklang_an(self):
        from mimic.effekte import Hall

        eingang, ausgang = self.sprich({"hall": 0.6})
        self.assertEqual(len(eingang) + 2 * (int(48_000 * Hall.DAUER_S) - 1), len(ausgang))
        self.assertNotEqual(eingang, ausgang[:len(eingang)])

    def test_ohne_hall_bleibt_die_laenge_stehen(self):
        eingang, ausgang = self.sprich({"hall": 0.0})
        self.assertEqual(eingang, ausgang)

    def test_regler_kommt_auf_den_profilwert_obendrauf(self):
        _eingang, ueber_regler = self.sprich({"hall": 0.5})
        _eingang, ueber_profil = self.sprich({}, stimme={"hall": 0.5})
        self.assertEqual(ueber_profil, ueber_regler)

    def test_unsinn_faellt_auf_trocken_zurueck(self):
        eingang, ausgang = self.sprich({"hall": "viel"})
        self.assertEqual(eingang, ausgang)


class SprachParameterTests(unittest.TestCase):
    """Die Stimmeinstellungen muessen bis an dots.tts durchkommen.

    Vorher stand `language="en"` hart im Worker und `speaker_scale` gar nicht --
    damit lief jede Stimme auf dem Default 1.5. Fuer eine englische Referenz mit
    deutschem Text ist das der Unterschied zwischen verstaendlich und nicht.
    """

    def test_worker_reicht_sprache_und_scale_durch(self):
        runtime = StubRuntime([[pcm(9000)]])
        run_engine(runtime, {"text": "Ein Satz, der lang genug ist."},
                   stimme={"language": "de", "speaker_scale": 0.8})
        self.assertEqual(1, len(runtime.kwargs))
        self.assertEqual("de", runtime.kwargs[0]["language"])
        self.assertEqual(0.8, runtime.kwargs[0]["speaker_scale"])

    def test_vorgabe_bleibt_der_gemessene_betriebspunkt(self):
        runtime = StubRuntime([[pcm(9000)]])
        run_engine(runtime, {"text": "Ein Satz, der lang genug ist."})
        self.assertEqual("en", runtime.kwargs[0]["language"])
        self.assertEqual(1.5, runtime.kwargs[0]["speaker_scale"])


class GuiAuthTests(unittest.TestCase):
    """Die vier Ladewege, die der Browser selbst anstoesst.

    Beim Umbau auf Double Submit wurde der `?t=`-Weg aus _erlaubt entfernt.
    Ein <audio src=...> und ein Download ueber location.href setzen aber keinen
    X-Mimic-Token-Kopf -- danach antworteten Referenz, Aufnahme, Entwurf und
    Export mit 403. Die Suite hat das nicht bemerkt, weil sie durchweg mit Kopf
    anfragt; deshalb fragt dieser Test wie ein Browser: nur mit Cookie.
    """

    def setUp(self):
        import http.server

        from mimic import gui

        self.sitzung = gui.Sitzung()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), gui.handler_klasse(self.sitzung))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _anfrage(self, pfad, kopf=None, methode="GET"):
        import http.client

        verbindung = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            verbindung.request(methode, pfad, headers=kopf or {})
            antwort = verbindung.getresponse()
            antwort.read()
            return antwort.status, antwort.getheader("Set-Cookie") or ""
        finally:
            verbindung.close()

    def _anmelden(self) -> str:
        status, keks = self._anfrage(f"/?t={self.sitzung.start_token}")
        self.assertEqual(200, status)
        return keks.split(";", 1)[0]

    def test_start_token_gilt_genau_einmal(self):
        start = self.sitzung.start_token
        self.assertEqual(200, self._anfrage(f"/?t={start}")[0])
        # Es steht in der Chromium-Kommandozeile, und /proc/<pid>/cmdline ist
        # fremd lesbar -- wer es dort spaeter abliest, haelt einen toten Wert.
        self.assertEqual(403, self._anfrage(f"/?t={start}")[0])

    def test_seite_laedt_mit_cookie_neu(self):
        cookie = self._anmelden()
        self.assertEqual(200, self._anfrage("/", {"Cookie": cookie})[0])

    def test_browser_ladewege_kommen_mit_cookie_durch(self):
        cookie = self._anmelden()
        # 404 ist in Ordnung -- ohne Aufnahme gibt es keine Datei. Verboten
        # waere 403: dann hinge die Wiedergabe wieder am fehlenden Kopf.
        for pfad in ("/api/state", "/api/take", "/api/reference?name=gibtsnicht",
                     "/api/design/audio?nummer=1", "/api/export"):
            with self.subTest(pfad=pfad):
                status, _ = self._anfrage(pfad, {"Cookie": cookie})
                self.assertNotEqual(403, status)

    def test_ohne_cookie_und_ohne_kopf_bleibt_zu(self):
        for pfad in ("/api/state", "/api/take", "/api/export"):
            with self.subTest(pfad=pfad):
                self.assertEqual(403, self._anfrage(pfad)[0])
        # Das alte Verfahren ist tot: das Token in der URL oeffnet nichts mehr.
        self.assertEqual(403, self._anfrage(f"/api/state?t={self.sitzung.token}")[0])

    def test_post_verlangt_weiter_den_kopf(self):
        cookie = self._anmelden()
        # Hier sitzt die Zustandsaenderung, also greift Double Submit: das
        # Cookie allein haengt der Browser auch an eine fremde Anfrage.
        self.assertEqual(403, self._anfrage("/api/stop", {"Cookie": cookie}, "POST")[0])
        status, _ = self._anfrage("/api/stop", {"X-Mimic-Token": self.sitzung.token}, "POST")
        self.assertNotEqual(403, status)
