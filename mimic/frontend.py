"""CPU-Frontend: validiert, begrenzt und reicht Worker-Rahmen weiter."""

from __future__ import annotations

import http.client
import json
import os
import select
import socket
import socketserver
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .protocol import MEDIA_TYPE, finish_chunks, read_frame, write_chunk
from .voices import VoiceError, close_voice, load_voice

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
}


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
        self._json(REASON_STATUS[reason], {"reason": reason, "message": message, **details})

    def do_GET(self) -> None:
        if self.path != "/status":
            self._json(404, {"reason": "bad_request", "message": "unbekannter Endpunkt"})
            return
        status_path = runtime_dir() / "worker-status.json"
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
            pid = value.get("worker_pid")
            if not isinstance(pid, int):
                raise ValueError
            os.kill(pid, 0)
        except (OSError, ValueError, json.JSONDecodeError):
            value = {"state": "kalt", "mode": None, "queue": 0, "worker_pid": None,
                     "last_load_s": None, "vram_free_mib": None, "uptime_s": 0}
        self._json(200, value)

    def do_POST(self) -> None:
        if self.path != "/speak":
            self._json(404, {"reason": "bad_request", "message": "unbekannter Endpunkt"})
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
        if not isinstance(request, dict) or not isinstance(request.get("text"), str) or not request["text"]:
            self._error("bad_request", "text fehlt oder ist leer")
            return
        if len(request["text"]) > MAX_TEXT_CHARS:
            self._error("text_too_long", f"text ueberschreitet {MAX_TEXT_CHARS} Zeichen")
            return
        mode = request.get("mode", "mf")
        voice = request.get("voice", "matthias")
        aussprache = request.get("aussprache", True)
        correlation_id = request.get("correlation_id")
        if mode not in ("mf", "soar"):
            self._error("bad_request", "mode muss mf oder soar sein")
            return
        if type(aussprache) is not bool:
            self._error("bad_request", "aussprache muss true oder false sein")
            return
        if correlation_id is None:
            correlation_id = uuid.uuid4().hex
        elif (not isinstance(correlation_id, str) or len(correlation_id) != 32
              or any(c not in "0123456789abcdefABCDEF" for c in correlation_id)):
            self._error("bad_request", "correlation_id muss 32-stelliges Hex sein")
            return
        try:
            profile = load_voice(voice)
        except VoiceError as exc:
            self._error(exc.reason, exc.message)
            return
        else:
            close_voice(profile)
        request = {"text": request["text"], "voice": voice, "mode": mode,
                   "aussprache": aussprache, "correlation_id": correlation_id}
        self._proxy(request)

    def _proxy(self, request: dict) -> None:
        for retry in range(2):
            if not self._proxy_once(request, retry_mode=(retry == 0)):
                return
        self._error("worker_unavailable", "Worker-Neustart war nicht erfolgreich")

    def _proxy_once(self, request: dict, *, retry_mode: bool) -> bool:
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        conn = UnixHTTPConnection(worker_socket_path(), CONNECT_TIMEOUT)
        try:
            conn.request("POST", "/synthesize", body, {"Content-Type": "application/json",
                                                         "Content-Length": str(len(body))})
            assert conn.sock is not None
            conn.sock.settimeout(HEADER_TIMEOUT)
            response = conn.getresponse()
        except socket.timeout:
            conn.close()
            self._error("worker_timeout", "Worker-Antwortkopf blieb aus")
            return False
        except (OSError, http.client.HTTPException):
            conn.close()
            self._error("worker_unavailable", "Worker ist nicht erreichbar")
            return False
        if response.status != 200:
            error: dict = {}
            try:
                error = json.loads(response.read(MAX_BODY_BYTES))
                reason = error.get("reason", "worker_unavailable")
                message = error.get("message", "Worker hat abgelehnt")
            except (json.JSONDecodeError, UnicodeDecodeError):
                reason, message = "worker_unavailable", "ungueltige Worker-Antwort"
            response.close()
            conn.close()
            details = {"hub_reason": error["hub_reason"]} if "hub_reason" in error else {}
            self._error(reason if reason in REASON_STATUS else "worker_unavailable", message,
                        **details)
            return False

        buffered: list[tuple[str, bytes]] = []
        audio_seen = False
        try:
            _set_response_timeout(response, FIRST_AUDIO_TIMEOUT)
            while not audio_seen:
                kind, payload = read_frame(response)
                buffered.append((kind, payload))
                if kind == "A":
                    audio_seen = True
                elif kind == "E":
                    end = json.loads(payload)
                    reason = end.get("reason", "worker_unavailable")
                    response.close()
                    conn.close()
                    if reason == "mode_restart" and retry_mode:
                        if _wait_worker_exit(end.get("worker_pid")):
                            return True
                        self._error("worker_unavailable", "Worker-Neustart blieb aus")
                        return False
                    details = {"hub_reason": end["hub_reason"]} if "hub_reason" in end else {}
                    self._error(reason if reason in REASON_STATUS else "worker_unavailable",
                                end.get("message", "Worker hat vor dem Stream abgebrochen"),
                                **details)
                    return False
        except socket.timeout:
            response.close()
            conn.close()
            self._error("worker_timeout", "erstes Audio blieb aus")
            return False
        except (OSError, EOFError, ValueError, http.client.HTTPException):
            response.close()
            conn.close()
            self._error("worker_unavailable", "Worker starb vor dem Stream")
            return False

        self.send_response(200)
        self.send_header("Content-Type", MEDIA_TYPE)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        samples = 0
        try:
            for kind, payload in buffered:
                from .protocol import encode_frame
                write_chunk(self.wfile, encode_frame(kind, payload))
                if kind == "A":
                    samples += len(payload) // 2
                if self._client_disconnected():
                    raise ConnectionResetError
            _set_response_timeout(response, FRAME_TIMEOUT)
            while buffered[-1][0] != "E":
                kind, payload = read_frame(response)
                from .protocol import encode_frame
                write_chunk(self.wfile, encode_frame(kind, payload))
                if self._client_disconnected():
                    raise ConnectionResetError
                buffered.append((kind, b""))
            finish_chunks(self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            # Schliessen der Worker-Verbindung ist das Cancel-Signal. Der Worker
            # prueft es nach jedem Yield und schliesst den Generator im finally.
            pass
        except socket.timeout:
            self._stream_error("worker_timeout", "Abstand zwischen Rahmen ueberschritten", samples)
        except (OSError, EOFError, http.client.HTTPException):
            self._stream_error("worker_unavailable", "Worker starb im Stream", samples)
        finally:
            response.close()
            conn.close()
        return False

    def _client_disconnected(self) -> bool:
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
            write_chunk(self.wfile, json_frame("E", {"status": "error", "reason": reason,
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
