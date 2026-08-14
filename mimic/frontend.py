"""CPU-Frontend: validiert, begrenzt und reicht Worker-Rahmen weiter."""

from __future__ import annotations

import http.client
import json
import os
import queue
import select
import socket
import socketserver
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .protocol import MEDIA_TYPE, finish_chunks, read_frame, write_chunk
from .voices import VoiceError, available_voices, close_voice, load_voice

MAX_TEXT_CHARS = int(os.environ.get("MIMIC_MAX_TEXT_CHARS", "1000"))
MAX_BODY_BYTES = int(os.environ.get("MIMIC_MAX_BODY_BYTES", str(64 * 1024)))
CONNECT_TIMEOUT = 2.0
HEADER_TIMEOUT = 5.0
FIRST_AUDIO_TIMEOUT = 90.0
FRAME_TIMEOUT = 10.0
REASON_STATUS = {
    "bad_request": 400, "text_too_long": 400, "unknown_voice": 404,
    "invalid_voice_profile": 422, "busy": 429, "insufficient_vram": 503,
    "worker_unavailable": 503, "worker_timeout": 504, "load_denied": 503,
    "cold": 503, "warm_in_progress": 409,
}
ERROR_DETAIL_FIELDS = {"hub_reason"}
READER_JOIN_TIMEOUT = 0.5


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "mimic"


def frontend_socket_path() -> Path:
    return Path(os.environ.get("MIMIC_SOCKET", runtime_dir() / "mimic.socket"))


def worker_socket_path() -> Path:
    return Path(os.environ.get("MIMIC_WORKER_SOCKET", runtime_dir() / "mimic-worker.socket"))


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float, cancelled=None):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)
        self.cancelled = cancelled

    def connect(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise OSError("Verbindung abgebrochen")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        self.sock = sock
        try:
            if self.cancelled is not None and self.cancelled():
                raise OSError("Verbindung abgebrochen")
            sock.connect(self.path)
        except BaseException:
            sock.close()
            self.sock = None
            raise


def _set_response_timeout(response: http.client.HTTPResponse, timeout: float) -> None:
    """http.client kann den Socket bei `Connection: close` ins Response verschieben."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        raise OSError("Worker-Socket ist nicht mehr offen")
    sock.settimeout(timeout)


def _wait_worker_exit(pid: object, timeout: float = CONNECT_TIMEOUT) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        time.sleep(0.01)
    return False


class _ConsumerDisconnected(Exception):
    pass


class _WorkerReader:
    """Liest HTTP und Rahmen im eigenen Thread, also inklusive Puffer von http.client."""

    def __init__(self, body: bytes):
        self.conn = UnixHTTPConnection(worker_socket_path(), CONNECT_TIMEOUT)
        self.body = body
        self.events: queue.Queue[tuple] = queue.Queue(maxsize=4)
        self.stopped = threading.Event()
        self.response: http.client.HTTPResponse | None = None
        self.thread = threading.Thread(target=self._run, name="mimic-worker-reader", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _put(self, event: tuple) -> bool:
        while not self.stopped.is_set():
            try:
                self.events.put(event, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        stage = "header"
        try:
            self.conn.request("POST", "/synthesize", self.body,
                              {"Content-Type": "application/json",
                               "Content-Length": str(len(self.body))})
            assert self.conn.sock is not None
            self.conn.sock.settimeout(HEADER_TIMEOUT)
            self.response = self.conn.getresponse()
            if self.response.status != 200:
                raw = self.response.read(MAX_BODY_BYTES)
                self._put(("http_error", self.response.status, raw))
                return
            if not self._put(("response", self.response)):
                return
            stage = "first_audio"
            _set_response_timeout(self.response, FIRST_AUDIO_TIMEOUT)
            audio_seen = False
            while not self.stopped.is_set():
                kind, payload = read_frame(self.response)
                if not self._put(("frame", kind, payload)):
                    return
                if kind == "A" and not audio_seen:
                    audio_seen = True
                    stage = "frame"
                    _set_response_timeout(self.response, FRAME_TIMEOUT)
                if kind == "E":
                    return
        except BaseException as exc:
            self._put(("reader_error", stage, exc))

    def close(self) -> None:
        self.stopped.set()
        sockets = []
        raw = getattr(getattr(self.response, "fp", None), "raw", None)
        response_sock = getattr(raw, "_sock", None)
        if response_sock is not None:
            sockets.append(response_sock)
        if self.conn.sock is not None and self.conn.sock not in sockets:
            sockets.append(self.conn.sock)
        # shutdown vor close weckt den Leser auch dann, wenn er bereits in einem
        # gepufferten HTTP-read steckt; ein nacktes close ist dafuer nicht verlaesslich.
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
        if self.response is not None:
            self.response.close()
        self.conn.close()
        if self.thread is not threading.current_thread():
            self.thread.join(READER_JOIN_TIMEOUT)


class FrontendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Mimic/1"

    def log_message(self, fmt: str, *args: object) -> None:
        # BaseHTTPRequestHandler erwartet hier ein TCP-Adresspaar. AF_UNIX liefert
        # fuer anonyme Gegenstellen dagegen einen leeren String.
        print(f"frontend=unix message={fmt % args}", file=sys.stderr)

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, reason: str, message: str, **details: object) -> None:
        allowed = {key: value for key, value in details.items() if key in ERROR_DETAIL_FIELDS}
        self._json(REASON_STATUS.get(reason, 503),
                   {"v": 1, "reason": reason, "message": message, **allowed})

    def do_GET(self) -> None:
        if self.path != "/status":
            self._error("bad_request", "unbekannter Endpunkt")
            return
        status_path = runtime_dir() / "worker-status.json"
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            pid = value.get("worker_pid")
            if not isinstance(pid, int):
                raise ValueError
            os.kill(pid, 0)
        except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
            value = {"v": 1, "state": "kalt", "mode": None, "queue": 0, "worker_pid": None,
                     "last_load_s": None, "vram_free_mib": None, "uptime_s": 0}
        value["v"] = 1
        value["voices"] = available_voices()
        self._json(200, value)

    def do_POST(self) -> None:
        if self.path not in {"/speak", "/warm"}:
            self._error("bad_request", "unbekannter Endpunkt")
            return
        length_raw = self.headers.get("Content-Length")
        try:
            length = int(length_raw or "")
        except ValueError:
            self._error("bad_request", "Content-Length fehlt oder ist ungueltig")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._error("bad_request", "JSON-Body ist zu gross")
            return
        try:
            request = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error("bad_request", "JSON ist ungueltig")
            return
        if not isinstance(request, dict):
            self._error("bad_request", "JSON muss ein Objekt sein")
            return
        if self.path == "/warm":
            self._handle_warm(request)
            return
        if not isinstance(request.get("text"), str) or not request["text"]:
            self._error("bad_request", "text fehlt oder ist leer")
            return
        if len(request["text"]) > MAX_TEXT_CHARS:
            self._error("text_too_long", f"text ueberschreitet {MAX_TEXT_CHARS} Zeichen")
            return
        mode = request.get("mode", "soar")
        voice = request.get("voice", "matthias")
        aussprache = request.get("aussprache", True)
        require_warm = request.get("require_warm", False)
        correlation_id = request.get("correlation_id")
        if mode not in ("mf", "soar"):
            self._error("bad_request", "mode muss mf oder soar sein")
            return
        if type(aussprache) is not bool:
            self._error("bad_request", "aussprache muss true oder false sein")
            return
        if type(require_warm) is not bool:
            self._error("bad_request", "require_warm muss true oder false sein")
            return
        if correlation_id is None:
            correlation_id = uuid.uuid4().hex
        elif (not isinstance(correlation_id, str) or len(correlation_id) != 32
              or any(c not in "0123456789abcdefABCDEF" for c in correlation_id)):
            self._error("bad_request", "correlation_id muss 32-stelliges Hex sein")
            return
        try:
            profile = load_voice(voice, mit_gain=False)
        except VoiceError as exc:
            self._error(exc.reason, exc.message)
            return
        else:
            close_voice(profile)
        request = {"text": request["text"], "voice": voice, "mode": mode,
                   "aussprache": aussprache, "require_warm": require_warm,
                   "correlation_id": correlation_id}
        self._proxy(request)

    def _handle_warm(self, request: dict) -> None:
        mode = request.get("mode", "soar")
        if mode not in ("mf", "soar"):
            self._error("bad_request", "mode muss mf oder soar sein")
            return
        correlation_id = request.get("correlation_id")
        if correlation_id is None:
            correlation_id = uuid.uuid4().hex
        elif (not isinstance(correlation_id, str) or len(correlation_id) != 32
              or any(c not in "0123456789abcdefABCDEF" for c in correlation_id)):
            self._error("bad_request", "correlation_id muss 32-stelliges Hex sein")
            return
        body = json.dumps({"mode": mode, "correlation_id": correlation_id},
                          separators=(",", ":")).encode()
        conn = UnixHTTPConnection(worker_socket_path(), CONNECT_TIMEOUT)
        try:
            conn.request("POST", "/warm", body, {"Content-Type": "application/json",
                                                  "Content-Length": str(len(body))})
            assert conn.sock is not None
            conn.sock.settimeout(HEADER_TIMEOUT)
            response = conn.getresponse()
            value = json.loads(response.read(MAX_BODY_BYTES))
            status = response.status
            response.close()
        except socket.timeout:
            self._error("worker_timeout", "Worker-Antwort auf Warmlauf blieb aus")
            return
        except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError):
            self._error("worker_unavailable", "Worker ist nicht erreichbar")
            return
        finally:
            conn.close()
        self._json(status, value)

    def _proxy(self, request: dict) -> None:
        for retry in range(2):
            if not self._proxy_once(request, retry_mode=(retry == 0)):
                return
        self._error("worker_unavailable", "Worker-Neustart war nicht erfolgreich")

    def _proxy_once(self, request: dict, *, retry_mode: bool) -> bool:
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        reader = _WorkerReader(body)
        reader.start()
        try:
            event = self._next_worker_event(reader, HEADER_TIMEOUT)
            if event[0] == "http_error":
                error: dict = {}
                try:
                    error = json.loads(event[2])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                reason = error.get("reason", "worker_unavailable")
                message = error.get("message", "Worker hat abgelehnt")
                details = {key: error[key] for key in ERROR_DETAIL_FIELDS if key in error}
                self._error(reason, message, **details)
                return False
            if event[0] == "reader_error":
                self._reader_error(event[1], event[2], before_audio=True)
                return False
            if event[0] != "response":
                raise ValueError("Worker-Antwortkopf fehlt")

            buffered: list[tuple[str, bytes]] = []
            audio_seen = False
            while not audio_seen:
                event = self._next_worker_event(reader, FIRST_AUDIO_TIMEOUT)
                if event[0] == "reader_error":
                    self._reader_error(event[1], event[2], before_audio=True)
                    return False
                if event[0] != "frame":
                    raise ValueError("ungueltiges Worker-Ereignis")
                _, kind, payload = event
                buffered.append((kind, payload))
                if kind == "A":
                    audio_seen = True
                elif kind == "E":
                    end = json.loads(payload)
                    reason = end.get("reason", "worker_unavailable")
                    if reason == "mode_restart" and retry_mode:
                        reader.close()
                        if _wait_worker_exit(end.get("worker_pid")):
                            return True
                        self._error("worker_unavailable", "Worker-Neustart blieb aus")
                        return False
                    details = {key: end[key] for key in ERROR_DETAIL_FIELDS if key in end}
                    self._error(reason,
                                end.get("message", "Worker hat vor dem Stream abgebrochen"),
                                **details)
                    return False

            self.send_response(200)
            self.send_header("Content-Type", MEDIA_TYPE)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            samples = 0
            for kind, payload in buffered:
                from .protocol import encode_frame
                write_chunk(self.wfile, encode_frame(kind, payload))
                if kind == "A":
                    samples += len(payload) // 2
                if self._client_disconnected():
                    raise ConnectionResetError
            while buffered[-1][0] != "E":
                event = self._next_worker_event(reader, FRAME_TIMEOUT)
                if event[0] == "reader_error":
                    self._reader_error(event[1], event[2], before_audio=False, samples=samples)
                    return False
                _, kind, payload = event
                from .protocol import encode_frame
                write_chunk(self.wfile, encode_frame(kind, payload))
                if self._client_disconnected():
                    raise ConnectionResetError
                buffered.append((kind, b""))
            finish_chunks(self.wfile)
        except _ConsumerDisconnected:
            pass
        except (BrokenPipeError, ConnectionResetError):
            # Schliessen der Worker-Verbindung ist das Cancel-Signal. Der Worker
            # prueft es nach jedem Yield und schliesst den Generator im finally.
            pass
        except (OSError, EOFError, ValueError, http.client.HTTPException):
            self._error("worker_unavailable", "Worker starb vor dem Stream")
        finally:
            reader.close()
        return False

    def _next_worker_event(self, reader: _WorkerReader, timeout: float) -> tuple:
        deadline = time.monotonic() + timeout
        while True:
            if self._client_disconnected():
                raise _ConsumerDisconnected
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ("reader_error", "timeout", socket.timeout())
            try:
                return reader.events.get(timeout=min(0.02, remaining))
            except queue.Empty:
                continue

    def _reader_error(self, stage: str, exc: BaseException, *, before_audio: bool,
                      samples: int = 0) -> None:
        if isinstance(exc, socket.timeout) or stage == "timeout":
            if before_audio:
                message = ("Worker-Antwortkopf blieb aus" if stage == "header"
                           else "erstes Audio blieb aus")
                self._error("worker_timeout", message)
            else:
                self._stream_error("worker_timeout", "Abstand zwischen Rahmen ueberschritten", samples)
        elif before_audio:
            self._error("worker_unavailable", "Worker starb vor dem Stream")
        else:
            self._stream_error("worker_unavailable", "Worker starb im Stream", samples)

    def _client_disconnected(self) -> bool:
        if not hasattr(self, "connection"):
            return False
        readable, _, _ = select.select([self.connection], [], [], 0)
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, OSError):
            return True

    def _stream_error(self, reason: str, message: str, samples: int) -> None:
        from .protocol import json_frame
        try:
            write_chunk(self.wfile, json_frame("E", {"v": 1, "status": "error", "reason": reason,
                                                       "message": message, "samples": samples}))
            finish_chunks(self.wfile)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def _server(handler: type[BaseHTTPRequestHandler], path: Path) -> ThreadingUnixServer:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    inherited = int(os.environ.get("LISTEN_FDS", "0")) >= 1 and int(os.environ.get("LISTEN_PID", "0")) == os.getpid()
    if inherited:
        server = ThreadingUnixServer(None, handler, bind_and_activate=False)
        server.socket = socket.socket(fileno=3)
        server.server_address = server.socket.getsockname()
        server.server_activate()
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = ThreadingUnixServer(str(path), handler)
        os.chmod(path, 0o600)
    return server


def main() -> int:
    with _server(FrontendHandler, frontend_socket_path()) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
