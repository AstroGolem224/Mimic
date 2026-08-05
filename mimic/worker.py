"""Einziger GPU-Eigentuemer, Prioritaetswarteschlange und Modelllebenszeit."""

from __future__ import annotations

import itertools
import json
import os
import queue
import resource
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .frontend import ThreadingUnixServer, _server, runtime_dir, worker_socket_path
from .protocol import finish_chunks, write_chunk, encode_frame
from .voices import (VoiceError, apply_pronunciation, close_voice, load_voice,
                     split_sentences)

REVISIONS = {
    "mf": ("dots-studio/dots.tts-mf", "25c53fb462e57087e52237daa5ea30df1c5cc328"),
    "soar": ("dots-studio/dots.tts-soar", "e3520f75254d0020a0406db31c51a79d00d22d55"),
}
MIN_VRAM_MIB = int(os.environ.get("MIMIC_MIN_VRAM_MIB", "8000"))
IDLE_TIMEOUT = float(os.environ.get("MIMIC_IDLE_TIMEOUT", "300"))
REQUEST_TIMEOUT = float(os.environ.get("MIMIC_REQUEST_TIMEOUT", "120"))
MAX_WAITING = 4
PAUSE_MS = int(os.environ.get("MIMIC_PAUSE_MS", "180"))  # Atempause zwischen Saetzen
SOFT_LIMIT = 0.75      # ab hier rollt der weiche Anschlag ein


class WorkerRefusal(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


class FatalWorkerError(WorkerRefusal):
    """Der Modellzustand ist nicht mehr vertrauenswuerdig; Prozess muss enden."""


@dataclass(order=True)
class Job:
    priority: int
    sequence: int
    request: dict = field(compare=False)
    events: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2), compare=False)
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)
    delivered: threading.Event = field(default_factory=threading.Event, compare=False)
    submitted: float = field(default_factory=time.monotonic, compare=False)


class Engine:
    def __init__(self) -> None:
        self.jobs: queue.PriorityQueue[Job] = queue.PriorityQueue(MAX_WAITING)
        self.sequence = itertools.count()
        self.runtimes: dict[str, object] = {}
        self.started = time.monotonic()
        self.last_load_s: float | None = None
        self.mode: str | None = None
        self.fatal = threading.Event()
        self.owner = threading.Thread(target=self._run, name="mimic-model-owner", daemon=True)
        self.owner.start()
        self.write_status("kalt")

    def submit(self, request: dict) -> Job:
        job = Job(0 if request["mode"] == "mf" else 1, next(self.sequence), request)
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            raise WorkerRefusal("busy", "Warteschlange ist voll") from None
        self.write_status("warm" if self.runtimes else "kalt")
        return job

    def emit(self, job: Job, kind: str, payload: bytes) -> bool:
        while not job.cancelled.is_set():
            try:
                job.events.put((kind, payload), timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                self._execute(job)
            except Exception as exc:
                reason = exc.reason if isinstance(exc, WorkerRefusal) else "worker_unavailable"
                message = exc.message if isinstance(exc, WorkerRefusal) else f"Worker-Fehler: {exc}"
                self.emit(job, "E", json.dumps({"status": "error", "reason": reason,
                                                "message": message, "samples": 0},
                                               ensure_ascii=False, separators=(",", ":")).encode())
                job.delivered.wait(1)
                self.fatal.set()
                wake_listener()
            finally:
                self.jobs.task_done()
                self.write_status("warm" if self.runtimes else "kalt")

    def _execute(self, job: Job) -> None:
        request_id = uuid.uuid4().hex
        request = job.request
        mode, voice = request["mode"], request["voice"]
        started = time.monotonic()
        queue_wait_ms = (started - job.submitted) * 1000
        samples = 0
        first_audio_at: float | None = None
        outcome, reason = "error", "worker_unavailable"
        profile = None
        generator = None
        cold = mode not in self.runtimes
        try:
            profile = load_voice(voice)
            if cold:
                self.write_status("laedt", mode)
                self.runtimes[mode] = self._load(mode)
            runtime = self.runtimes[mode]
            self.mode = mode
            sample_rate = int(runtime.sample_rate)
            head = {"v": 1, "sample_rate": sample_rate, "channels": 1, "format": "s16le",
                    "request_id": request_id, "mode": mode, "voice": voice}
            if not self.emit(job, "H", json.dumps(head, separators=(",", ":")).encode()):
                outcome = "cancelled"
                return
            text = apply_pronunciation(request["text"])
            # Satzweise statt am Stueck: sonst haengen die Saetze ohne Atempause
            # aneinander. Die Zerlegung fasst kurze Bruchstuecke wieder zusammen
            # -- Fragmente ohne Satzkontext halluziniert das Modell voll.
            saetze = split_sentences(text) or [text]
            pause = bytes(int(sample_rate * PAUSE_MS / 1000) * 2)
            for index, satz in enumerate(saetze):
                if index:
                    if not self.emit(job, "A", pause):
                        outcome = "cancelled"
                        return
                    samples += len(pause) // 2
                generator = runtime.generate_stream(text=satz, language="en",
                                                    prompt_audio_path=profile.wav_path,
                                                    prompt_text=profile.prompt_text)
                for chunk in generator:
                    if job.cancelled.is_set():
                        outcome = "cancelled"
                        return
                    if time.monotonic() - started > REQUEST_TIMEOUT:
                        raise WorkerRefusal("worker_timeout", "Wanduhrfrist von 120 s gerissen")
                    pcm = tensor_to_pcm(chunk, profile.gain)
                    if first_audio_at is None:
                        first_audio_at = time.monotonic()
                    samples += len(pcm) // 2
                    if not self.emit(job, "A", pcm):
                        outcome = "cancelled"
                        return
                generator.close()
                generator = None
            self.emit(job, "E", json.dumps({"status": "ok", "samples": samples},
                                            separators=(",", ":")).encode())
            outcome, reason = "ok", ""
        except FatalWorkerError as exc:
            reason = exc.reason
            self.emit(job, "E", json.dumps({"status": "error", "reason": exc.reason,
                                             "message": exc.message, "samples": samples},
                                            ensure_ascii=False, separators=(",", ":")).encode())
            job.delivered.wait(1)
            self.fatal.set()
            wake_listener()
        except WorkerRefusal as exc:
            reason = exc.reason
            self.emit(job, "E", json.dumps({"status": "error", "reason": exc.reason,
                                             "message": exc.message, "samples": samples},
                                            ensure_ascii=False, separators=(",", ":")).encode())
        except VoiceError as exc:
            reason = exc.reason
            self.emit(job, "E", json.dumps({"status": "error", "reason": exc.reason,
                                             "message": exc.message, "samples": samples},
                                            ensure_ascii=False, separators=(",", ":")).encode())
        except Exception as exc:
            reason = "worker_unavailable"
            self.emit(job, "E", json.dumps({"status": "error", "reason": reason,
                                             "message": f"Worker-Fehler: {exc}", "samples": samples},
                                            ensure_ascii=False, separators=(",", ":")).encode())
            job.delivered.wait(1)
            self.fatal.set()
            wake_listener()
        finally:
            if generator is not None:
                close = getattr(generator, "close", None)
                if close:
                    close()
            if profile is not None:
                close_voice(profile)
            elapsed = time.monotonic() - started
            audio_s = samples / int(getattr(self.runtimes.get(mode), "sample_rate", 48_000))
            rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            fields = {"request_id": request_id, "voice": voice, "mode": mode,
                      "chars": len(request["text"]), "state": "kalt" if cold else "warm",
                      "load_s": self.last_load_s or 0,
                      "ttfa_ms": round((first_audio_at - started) * 1000, 1) if first_audio_at else 0,
                      "audio_s": round(audio_s, 3), "rtf": round(elapsed / audio_s, 4) if audio_s else 0,
                      "outcome": outcome, "queue_wait_ms": round(queue_wait_ms, 1),
                      "vram_peak_mib": peak_vram_mib(), "rss_peak_mib": round(rss_mib, 1)}
            if reason:
                fields["reason"] = reason
            print(" ".join(f"{key}={value}" for key, value in fields.items()), flush=True)

    def _load(self, mode: str):
        free_mib = vram_free_mib()
        if free_mib < MIN_VRAM_MIB:
            # Bewusst KEIN FatalWorkerError: knapper VRAM ist ein erwarteter,
            # voruebergehender Zustand (ComfyUI laeuft), kein beschaedigter
            # Modellzustand. Ein Prozessende hier wuerde ein bereits geladenes,
            # funktionierendes Modell mitreissen -- etwa wenn mf bedient und
            # nebenher soar angefragt wird.
            raise WorkerRefusal("insufficient_vram",
                                f"nur {free_mib} MiB VRAM frei, {MIN_VRAM_MIB} MiB erforderlich")
        marker = os.environ.get("MIMIC_FAKE_LOAD_MARKER")
        if marker:
            Path(marker).touch()
        release = request_gpu_permission()
        t0 = time.monotonic()
        try:
            import torch
            from dots_tts.runtime import DotsTtsRuntime
            repo, revision = REVISIONS[mode]
            os.environ["HF_HUB_OFFLINE"] = "1"
            # Direktes Konstruieren auf CUDA senkte die gemessene RAM-Spitze von
            # 12479 auf 5514 MiB; ausserhalb dieses Kontexts droht Kernel-OOM.
            with torch.device("cuda"):
                runtime = DotsTtsRuntime.from_pretrained(
                    repo, revision=revision, precision="bfloat16", optimize=False)
            self.last_load_s = time.monotonic() - t0
            return runtime
        except WorkerRefusal:
            raise
        except Exception as exc:
            raise FatalWorkerError("worker_unavailable", f"Modell konnte nicht laden: {exc}") from exc
        finally:
            release()

    def write_status(self, state: str, mode: str | None = None) -> None:
        path = runtime_dir() / "worker-status.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = {"state": state, "mode": mode or self.mode, "queue": self.jobs.qsize(),
                 "worker_pid": os.getpid(), "last_load_s": self.last_load_s,
                 "vram_free_mib": safe_vram_free_mib(),
                 "uptime_s": int(time.monotonic() - self.started)}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)


def vram_free_mib() -> int:
    fake = os.environ.get("MIMIC_VRAM_FREE_MIB")
    if fake is not None:
        return int(fake)
    import torch
    free, _total = torch.cuda.mem_get_info()
    return int(free // (1024 * 1024))


def safe_vram_free_mib() -> int | None:
    try:
        return vram_free_mib()
    except Exception:
        return None


def peak_vram_mib() -> int:
    try:
        import torch
        return int(torch.cuda.max_memory_allocated() // (1024 * 1024))
    except Exception:
        return 0


def tensor_to_pcm(chunk, gain: float = 1.0) -> bytes:
    """Verstaerkung kommt aus dem Stimmprofil und ist je Stimme konstant --
    deshalb hier ein Faktor und keine Normalisierung ueber die Aeusserung:
    letztere ginge im Stream nicht, ohne zwischen Chunks zu pumpen. Das
    clamp danach ist der Peak-Anschlag."""
    import torch
    audio = chunk.detach().squeeze().to(device="cpu", dtype=torch.float32)
    if gain != 1.0:
        audio = audio * gain
        # Weicher Anschlag statt hartem clamp. Unterhalb der Schwelle bleibt das
        # Signal unangetastet, darueber wird es per tanh eingerollt -- sonst
        # klaenge der lautere Zielpegel nach Uebersteuerung statt nach laut.
        laut = audio.abs()
        ueber = laut > SOFT_LIMIT
        if bool(ueber.any()):
            rest = 1.0 - SOFT_LIMIT
            gerollt = SOFT_LIMIT + rest * torch.tanh((laut - SOFT_LIMIT) / rest)
            audio = torch.where(ueber, torch.sign(audio) * gerollt, audio)
    return (audio.clamp(-1, 1) * 32767).to(torch.int16).numpy().astype("<i2", copy=False).tobytes()


def request_gpu_permission():
    """dAImons Hub ist optional; jedes Protokoll- oder Socketproblem faellt offen."""
    hub = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "daimon/gpu.sock"
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        sock.connect(str(hub))
        sock.sendall(b'{"action":"request","client":"mimic"}\n')
        if not sock.recv(4096):
            raise OSError("leere Hub-Antwort")
    except Exception:
        if sock is not None:
            sock.close()
        return lambda: None

    def release() -> None:
        try:
            sock.sendall(b"fertig\n")
        except OSError:
            pass
        finally:
            sock.close()
    return release


def wake_listener() -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
            wake.settimeout(0.2)
            wake.connect(str(worker_socket_path()))
    except OSError:
        pass


class WorkerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MimicWorker/1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def _error(self, status: int, reason: str, message: str) -> None:
        body = json.dumps({"reason": reason, "message": message}, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/synthesize":
            self._error(400, "bad_request", "unbekannter Endpunkt")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            request = json.loads(self.rfile.read(length))
            if request.get("mode") not in REVISIONS or not isinstance(request.get("text"), str):
                raise ValueError
            job = ENGINE.submit(request)
        except (ValueError, json.JSONDecodeError, AttributeError):
            self._error(400, "bad_request", "ungueltige interne Anfrage")
            return
        except WorkerRefusal as exc:
            self._error(429, exc.reason, exc.message)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.mimic.frames")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                kind, payload = job.events.get(timeout=REQUEST_TIMEOUT + 5)
                write_chunk(self.wfile, encode_frame(kind, payload))
                if kind == "E":
                    finish_chunks(self.wfile)
                    job.delivered.set()
                    return
        except (BrokenPipeError, ConnectionResetError, TimeoutError, queue.Empty):
            job.cancelled.set()
        finally:
            job.cancelled.set()


ENGINE: Engine


def main() -> int:
    global ENGINE
    ENGINE = Engine()
    path = worker_socket_path()
    with _server(WorkerHandler, path) as server:
        server.timeout = IDLE_TIMEOUT
        # Ein Timeout am horchenden Socket beendet main direkt. Das ist absichtlich
        # kein Timer-Thread: erst der Prozessausgang gibt RAM und VRAM sicher frei.
        while True:
            before = time.monotonic()
            server.handle_request()
            if ENGINE.fatal.is_set():
                return 0
            if time.monotonic() - before >= IDLE_TIMEOUT * 0.95:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
