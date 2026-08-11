"""Einziger GPU-Eigentuemer, Prioritaetswarteschlange und Modelllebenszeit."""

from __future__ import annotations

import itertools
import heapq
import json
import os
import queue
import resource
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .frontend import ThreadingUnixServer, _server, runtime_dir, worker_socket_path
from .effekte import Effekt
from .protocol import finish_chunks, write_chunk, encode_frame
from .voices import (VoiceError, apply_pronunciation, close_voice, endet_satz,
                     entschaerfe_versalien, load_voice, split_sentences)

REVISIONS = {
    "mf": ("dots-studio/dots.tts-mf", "25c53fb462e57087e52237daa5ea30df1c5cc328"),
    "soar": ("dots-studio/dots.tts-soar", "e3520f75254d0020a0406db31c51a79d00d22d55"),
}
MIN_VRAM_MIB = int(os.environ.get("MIMIC_MIN_VRAM_MIB", "8000"))
MODEL_VRAM_MIB = int(os.environ.get("MIMIC_MODEL_VRAM_MIB", "6222"))
IDLE_TIMEOUT = float(os.environ.get("MIMIC_IDLE_TIMEOUT", "300"))
REQUEST_TIMEOUT = float(os.environ.get("MIMIC_REQUEST_TIMEOUT", "120"))
MAX_WAITING = 4
PAUSE_MS = int(os.environ.get("MIMIC_PAUSE_MS", "180"))  # Atempause zwischen Saetzen
SOFT_LIMIT = 0.75      # ab hier rollt der weiche Anschlag ein

# Gemessen am 2026-08-05: dots.tts liefert bei kurzen Saetzen gelegentlich die
# volle Dauer zurueck, aber ohne Sprache darin -- 2 von 14 Laeufen mit "Wer
# kommt mit?" (14 Zeichen), unabhaengig davon ob der Satz allein steht oder in
# einer laengeren Aeusserung. Der Satz fehlt dann hoerbar. Die Spitze trennt
# beide Faelle klar: gesprochene Takes lagen bei -12.1 dBFS und darueber (die
# meisten bei -6.4, weil der weiche Anschlag sie dort haelt), stumme bei -31.5
# und -35.1. -25 dBFS liegt mit Abstand dazwischen. RMS taugt dafuer nicht:
# dort lagen die Faelle mit -32.3 gegen -36.9 zu dicht beieinander.
STUMM_PEAK = int(32768 * 10 ** (-25.0 / 20))
MAX_VERSUCHE = 2


class WorkerRefusal(Exception):
    def __init__(self, reason: str, message: str, **details: object):
        super().__init__(message)
        self.reason, self.message = reason, message
        self.details = details


class FatalWorkerError(WorkerRefusal):
    """Der Modellzustand ist nicht mehr vertrauenswuerdig; Prozess muss enden."""


class ModeSwitch(FatalWorkerError):
    """Die Anfrage muss nach dem kontrollierten Prozessende einmal wiederholt werden."""


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
        self.jobs: list[Job] = []
        self.sequence = itertools.count()
        self.runtimes: dict[str, object] = {}
        self.state = "cold"
        self.state_lock = threading.Lock()
        self.condition = threading.Condition(self.state_lock)
        self.warm_request: dict | None = None
        self.warming_mode: str | None = None
        self.loading_mode: str | None = None
        self.started = time.monotonic()
        self.last_load_s: float | None = None
        self.vram_free_mib: int | None = None
        self.mode: str | None = None
        self.fatal = threading.Event()
        # Der socket-aktivierte Prozess muss `require_warm` beantworten koennen,
        # bevor ein Torch-Import die gemessenen 0.69--0.75 s kostet. Deshalb ist
        # der erste Status absichtlich eine reine CPU-Datei mit VRAM null.
        self.write_status("kalt")
        self.owner = threading.Thread(target=self._run, name="mimic-model-owner", daemon=True)
        self.owner.start()

    def submit(self, request: dict) -> Job:
        job = Job(0 if request["mode"] == "mf" else 1, next(self.sequence), request)
        condition = getattr(self, "condition", None)
        if condition is None:
            # Kompatibilitaet fuer kleine, bewusst unvollstaendige Test-Engines.
            condition = threading.Condition(getattr(self, "state_lock", threading.Lock()))
            self.condition = condition
        with condition:
            state = getattr(self, "state", "warm" if self.runtimes else "cold")
            mode_warm = state == "warm" and request["mode"] in self.runtimes
            if state == "loading" or (request.get("require_warm", False) and not mode_warm):
                raise WorkerRefusal("cold", f"Modus {request['mode']} ist nicht warm")
            if len(self.jobs) >= MAX_WAITING:
                raise WorkerRefusal("busy", "Warteschlange ist voll")
            heapq.heappush(self.jobs, job)
            condition.notify()
        self.write_status("warm" if state == "warm" else "kalt")
        return job

    def request_warm(self, request: dict) -> int:
        mode = request.get("mode")
        if mode not in REVISIONS:
            raise WorkerRefusal("bad_request", f"unbekannter Modus fuer den Warmlauf: {mode!r}")
        with self.condition:
            if self.state == "warm" and mode in self.runtimes:
                return 200
            if self.warm_request is not None or self.warming_mode == mode or (
                    self.state == "loading" and self.loading_mode == mode):
                return 409
            self.warm_request = request
            self.condition.notify()
        self.write_status("kalt")
        return 202

    def emit(self, job: Job, kind: str, payload: bytes) -> bool:
        # Deckel gegen jeden Pfad, auf dem ein Verbraucher ohne gesetztes
        # cancelled verschwindet: der Owner-Thread ist einer fuer alle Jobs,
        # er darf nie unbegrenzt an einem einzigen haengen. Grosszuegig, weil
        # das Frontend einen lebenden Leser schon nach FRAME_TIMEOUT aufgibt.
        frist = time.monotonic() + REQUEST_TIMEOUT
        while not job.cancelled.is_set():
            if time.monotonic() > frist:
                job.cancelled.set()
                print(f"job=emit outcome=abandoned wartezeit_s={REQUEST_TIMEOUT:.0f}",
                      file=sys.stderr, flush=True)
                return False
            try:
                job.events.put((kind, payload), timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        while True:
            with self.condition:
                while not self.jobs and self.warm_request is None:
                    self.condition.wait()
                if self.jobs:
                    job = heapq.heappop(self.jobs)
                    warm_request = None
                else:
                    job = None
                    warm_request = self.warm_request
                    self.warm_request = None
                    self.warming_mode = warm_request["mode"]
            if job is None:
                self._warm(warm_request)
                continue
            try:
                self._execute(job)
            except Exception as exc:
                reason = exc.reason if isinstance(exc, WorkerRefusal) else "worker_unavailable"
                message = exc.message if isinstance(exc, WorkerRefusal) else f"Worker-Fehler: {exc}"
                details = exc.details if isinstance(exc, WorkerRefusal) else {}
                self.emit(job, "E", json.dumps({"v": 1, "status": "error", "reason": reason,
                                                "message": message, "samples": 0, **details},
                                               ensure_ascii=False, separators=(",", ":")).encode())
                job.delivered.wait(1)
                self.fatal.set()
                wake_listener()
            finally:
                self.write_status("warm" if self.state == "warm" else "kalt")

    def _warm(self, request: dict | None) -> None:
        if request is None:
            return
        mode = request["mode"]
        started = time.monotonic()
        outcome, reason = "ok", ""
        try:
            if self.state == "warm":
                if mode in self.runtimes:
                    return
                raise ModeSwitch("mode_restart", f"Moduswechsel zu {mode} braucht Worker-Neustart",
                                 worker_pid=os.getpid())
            self._publish_loading(mode)
            try:
                runtime = self._load(mode)
            except BaseException:
                with self.condition:
                    self.state = "cold"
                    self.loading_mode = None
                raise
            with self.condition:
                self.runtimes[mode] = runtime
                self.mode = mode
                self.state = "warm"
                self.loading_mode = None
            self._warm_audio_path(runtime, request.get("voice", "matthias"))
        except FatalWorkerError as exc:
            outcome, reason = "error", exc.reason
            self.fatal.set()
            wake_listener()
        except (WorkerRefusal, Exception) as exc:
            outcome = "error"
            reason = exc.reason if isinstance(exc, WorkerRefusal) else "worker_unavailable"
        finally:
            with self.condition:
                self.warming_mode = None
            self.write_status("warm" if self.state == "warm" else "kalt")
            fields = {"correlation_id": request.get("correlation_id"), "mode": mode,
                      "operation": "warm", "outcome": outcome,
                      "elapsed_s": round(time.monotonic() - started, 3)}
            if reason:
                fields["reason"] = reason
            print(" ".join(f"{key}={value}" for key, value in fields.items()), flush=True)

    def _warm_audio_path(self, runtime, voice: str) -> None:
        """Referenzaudio einmal durchschicken, damit der JIT bezahlt ist.

        Das Modell zu laden reicht nicht: librosa zieht beim ERSTEN Laden eines
        Referenzaudios numba durch die JIT-Kompilierung. Gemessen am 2026-08-05
        im frisch gewaermten Worker: 8.7 s allein fuer `_load_prompt_audio`,
        TTFA 10596 ms -- danach 377, 300, 217 ms. Ein Warmlauf, der diesen Teil
        auslaesst, meldet "warm" und laesst den naechsten Aufruf trotzdem zehn
        Sekunden warten. Fuer dAImon hiesse das: Frist gerissen, Rueckfall auf
        sherpa, Warmlauf umsonst.

        Fehler hier sind kein Grund, den Warmlauf zu verwerfen -- das Modell ist
        geladen, nur der Audio-Pfad ist noch kalt. Also protokollieren und weiter.
        """
        profile = None
        try:
            profile = load_voice(voice)
            # Dieselben Werte wie im Ernstfall: ein Warmlauf mit anderer Sprache
            # oder anderem speaker_scale waermt einen Pfad, der so nie laeuft.
            for _ in runtime.generate_stream(text="Warmlauf.", language=profile.language,
                                             speaker_scale=profile.speaker_scale,
                                             prompt_audio_path=profile.wav_path,
                                             prompt_text=profile.prompt_text):
                break          # der erste Chunk genuegt, der JIT ist damit durch
        except Exception as exc:
            print(f"warm=audiopfad outcome=error grund={type(exc).__name__}",
                  file=sys.stderr, flush=True)
        finally:
            if profile is not None:
                close_voice(profile)

    def _publish_loading(self, mode: str) -> None:
        with self.condition:
            self.state = "loading"
            self.loading_mode = mode
        self.write_status("laedt", mode)

    def _execute(self, job: Job) -> None:
        request = job.request
        request_id = uuid.uuid4().hex
        mode, voice = request["mode"], request["voice"]
        started = time.monotonic()
        queue_wait_ms = (started - job.submitted) * 1000
        samples = 0
        wiederholungen = 0
        first_audio_at: float | None = None
        # Diagnose zum TTFA-Schwanz: erster Chunk ueberhaupt gegen ersten hoerbaren.
        # Ohne die beiden Zahlen ist "Modell rechnet langsam" nicht von "Modell
        # erzeugt stille Vorlaufchunks" zu unterscheiden.
        first_chunk_at: float | None = None
        praefix_chunks = 0
        outcome, reason = "error", "worker_unavailable"
        profile = None
        generator = None
        cold = self.state != "warm"
        try:
            profile = load_voice(voice)
            if self.state == "warm" and mode not in self.runtimes:
                raise ModeSwitch("mode_restart", f"Moduswechsel zu {mode} braucht Worker-Neustart",
                                 worker_pid=os.getpid())
            if cold:
                self._publish_loading(mode)
                try:
                    runtime = self._load(mode)
                except BaseException:
                    with self.condition:
                        self.state = "cold"
                        self.loading_mode = None
                    raise
                with self.condition:
                    self.runtimes[mode] = runtime
                    self.state = "warm"
                    self.loading_mode = None
            runtime = self.runtimes[mode]
            self.mode = mode
            sample_rate = int(runtime.sample_rate)
            head = {"v": 1, "sample_rate": sample_rate, "channels": 1, "format": "s16le",
                    "request_id": request_id, "mode": mode, "voice": voice}
            if not self.emit(job, "H", json.dumps(head, separators=(",", ":")).encode()):
                outcome = "cancelled"
                return
            text = (apply_pronunciation(request["text"])
                    if request.get("aussprache", True) else request["text"])
            # Unabhaengig von der Aussprachetabelle: Versalien verhunzt das
            # Modell zuverlaessig, das ist keine Geschmacksfrage.
            text = entschaerfe_versalien(text)
            # Satzweise statt am Stueck: sonst haengen die Saetze ohne Atempause
            # aneinander. Die Zerlegung fasst kurze Bruchstuecke wieder zusammen
            # -- Fragmente ohne Satzkontext halluziniert das Modell voll.
            saetze = split_sentences(text) or [text]
            pause = bytes(int(sample_rate * PAUSE_MS / 1000) * 2)
            # Ein Effekt je Aeusserung, nicht je Block: Tremolo- und
            # Verzoegerungszustand muessen ueber Blockgrenzen tragen, sonst
            # klickt es an jeder Naht.
            effekt = Effekt(profile.effekt, sample_rate) if profile.effekt else None
            for index, satz in enumerate(saetze):
                # Pause nur, wo wirklich ein Satz endete. Die Schnitte an Komma
                # und Semikolon (voices.MAX_SATZ_ZEICHEN) sind Generierungs-
                # grenzen, keine Sprechpausen -- dort klang die Atempause wie
                # ein Aussetzer mitten im Satz.
                if index and endet_satz(saetze[index - 1]):
                    if not self.emit(job, "A", pause):
                        outcome = "cancelled"
                        return
                    samples += len(pause) // 2
                for versuch in range(MAX_VERSUCHE):
                    if job.cancelled.is_set():
                        outcome = "cancelled"
                        return
                    spitze = 0
                    anfang: list[bytes] = []
                    hoerbar = False
                    generator = runtime.generate_stream(text=satz, language=profile.language,
                                                        speaker_scale=profile.speaker_scale,
                                                        prompt_audio_path=profile.wav_path,
                                                        prompt_text=profile.prompt_text)
                    for chunk in generator:
                        if job.cancelled.is_set():
                            outcome = "cancelled"
                            return
                        if time.monotonic() - started > REQUEST_TIMEOUT:
                            raise WorkerRefusal("worker_timeout", "Wanduhrfrist von 120 s gerissen")
                        if first_chunk_at is None:
                            first_chunk_at = time.monotonic()
                        pcm = tensor_to_pcm(chunk, profile.gain)
                        if effekt is not None:
                            pcm = effekt.verarbeite(pcm)
                        spitze = max(spitze, peak_int16(pcm))
                        if not hoerbar:
                            anfang.append(pcm)
                            if spitze <= STUMM_PEAK:
                                continue
                            # Nur der letzte stille Chunk bleibt als Luft fuer
                            # weiche Anlaute. Der Rest wird verworfen: er kostet
                            # den Hoerer bis zu einer Sekunde Stille nach dem
                            # ersten Rahmen, ohne etwas zu tragen.
                            praefix_chunks = len(anfang) - 1
                            pcm = b"".join(anfang[-2:])
                            anfang.clear()
                            hoerbar = True
                            if first_audio_at is None:
                                first_audio_at = time.monotonic()
                        samples += len(pcm) // 2
                        if not self.emit(job, "A", pcm):
                            outcome = "cancelled"
                            return
                    generator.close()
                    generator = None
                    if hoerbar:
                        break
                    wiederholungen += 1
                else:
                    raise WorkerRefusal("silent_audio", "zwei stumme Takes erzeugt")
            self.emit(job, "E", json.dumps({"status": "ok", "samples": samples},
                                            separators=(",", ":")).encode())
            outcome, reason = "ok", ""
        except FatalWorkerError as exc:
            reason = exc.reason
            self.emit(job, "E", json.dumps({"v": 1, "status": "error", "reason": exc.reason,
                                             "message": exc.message, "samples": samples,
                                             **exc.details},
                                            ensure_ascii=False, separators=(",", ":")).encode())
            job.delivered.wait(1)
            self.fatal.set()
            wake_listener()
        except WorkerRefusal as exc:
            reason = exc.reason
            self.emit(job, "E", json.dumps({"v": 1, "status": "error", "reason": exc.reason,
                                             "message": exc.message, "samples": samples,
                                             **exc.details},
                                            ensure_ascii=False, separators=(",", ":")).encode())
        except VoiceError as exc:
            reason = exc.reason
            self.emit(job, "E", json.dumps({"v": 1, "status": "error", "reason": exc.reason,
                                             "message": exc.message, "samples": samples},
                                            ensure_ascii=False, separators=(",", ":")).encode())
        except Exception as exc:
            reason = "worker_unavailable"
            self.emit(job, "E", json.dumps({"v": 1, "status": "error", "reason": reason,
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
                      "vram_peak_mib": peak_vram_mib(), "rss_peak_mib": round(rss_mib, 1),
                      "stumme_takes": wiederholungen,
                      "erstchunk_ms": (round((first_chunk_at - started) * 1000, 1)
                                       if first_chunk_at else 0),
                      "praefix_chunks": praefix_chunks}
            if reason:
                fields["reason"] = reason
            print(" ".join(f"{key}={value}" for key, value in fields.items()), flush=True)

    def _load(self, mode: str):
        free_mib = vram_free_mib()
        self.vram_free_mib = free_mib
        if free_mib < MIN_VRAM_MIB:
            # Bewusst KEIN FatalWorkerError: knapper VRAM ist ein erwarteter,
            # voruebergehender Zustand (ComfyUI laeuft), kein beschaedigter
            # Modellzustand. Ein Prozessende hier wuerde ein bereits geladenes,
            # funktionierendes Modell mitreissen -- etwa wenn mf bedient und
            # nebenher soar angefragt wird.
            raise WorkerRefusal("insufficient_vram",
                                f"nur {free_mib} MiB VRAM frei, {MIN_VRAM_MIB} MiB erforderlich")
        release = request_gpu_permission()
        try:
            marker = os.environ.get("MIMIC_FAKE_LOAD_MARKER")
            if marker:
                Path(marker).touch()
            t0 = time.monotonic()
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
        value = {"v": 1, "state": state, "mode": mode or self.mode, "queue": len(self.jobs),
                 "worker_pid": os.getpid(), "last_load_s": self.last_load_s,
                 "vram_free_mib": self.vram_free_mib,
                 "uptime_s": int(time.monotonic() - self.started)}
        # Eindeutiger Zwischenname je Aufruf. Mit einem gemeinsamen ".tmp"
        # laufen zwei Schreiber ineinander: der erste benennt um, dem zweiten
        # fehlt die Quelle und os.replace wirft FileNotFoundError -- was den
        # Worker beim Start umbringt. Aufgetreten am 2026-08-05, nachdem die
        # Condition aus Punkt 4 mehr Nebenlaeufigkeit in write_status brachte.
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise


def vram_free_mib() -> int:
    fake = os.environ.get("MIMIC_VRAM_FREE_MIB")
    if fake is not None:
        return int(fake)
    import torch
    free, _total = torch.cuda.mem_get_info()
    return int(free // (1024 * 1024))


def peak_vram_mib() -> int:
    try:
        import torch
        return int(torch.cuda.max_memory_allocated() // (1024 * 1024))
    except Exception:
        return 0


def peak_int16(pcm: bytes) -> int:
    """Groesste Amplitude im Block -- das Kriterium fuer einen stummen Take."""
    import array
    werte = array.array("h")
    werte.frombytes(pcm)
    return max((abs(wert) for wert in werte), default=0)


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


def _hub_roundtrip(hub: Path, request: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(str(hub))
    except OSError:
        sock.close()
        raise
    try:
        sock.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        with sock.makefile("rb") as stream:
            raw = stream.readline(4097)
    except OSError as exc:
        raise WorkerRefusal("load_denied", "Hub-Antwort blieb aus",
                            hub_reason="hub_io_error") from exc
    finally:
        sock.close()
    if not raw:
        raise WorkerRefusal("load_denied", "Hub antwortete leer", hub_reason="hub_empty")
    try:
        response = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WorkerRefusal("load_denied", "Hub antwortete mit ungueltigem JSON",
                            hub_reason="hub_invalid_json") from None
    if not isinstance(response, dict) or response.get("v") != 1 or type(response.get("ok")) is not bool:
        raise WorkerRefusal("load_denied", "Hub-Antwort hat ein fremdes Schema",
                            hub_reason="hub_invalid_schema")
    return response


def request_gpu_permission():
    """Nur ein nicht erreichbarer Hub faellt offen; Antworten werden streng geprueft."""
    hub = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "daimon/gpu.sock"
    try:
        response = _hub_roundtrip(hub, {"v": 1, "art": "laden", "modell": "mimic",
                                        "vram_mib": MODEL_VRAM_MIB})
    except OSError:
        return lambda: None
    except WorkerRefusal as exc:
        print(f"hub=gpu outcome=denied hub_reason={exc.details.get('hub_reason')}",
              file=sys.stderr, flush=True)
        raise
    if not response["ok"]:
        reason = response.get("grund")
        if reason not in {"vram", "fullscreen", "lade_sperre"}:
            print("hub=gpu outcome=denied hub_reason=hub_invalid_schema",
                  file=sys.stderr, flush=True)
            raise WorkerRefusal("load_denied", "Hub-Antwort hat ein fremdes Schema",
                                hub_reason="hub_invalid_schema")
        raise WorkerRefusal("load_denied", f"Hub lehnt Laden ab: {reason}", hub_reason=reason)
    token = response.get("sperre")
    if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdefABCDEF" for c in token):
        print("hub=gpu outcome=denied hub_reason=hub_invalid_schema",
              file=sys.stderr, flush=True)
        raise WorkerRefusal("load_denied", "Hub-Antwort enthaelt keine gueltige Sperre",
                            hub_reason="hub_invalid_schema")

    def release() -> None:
        try:
            answer = _hub_roundtrip(hub, {"v": 1, "art": "fertig", "sperre": token})
            if answer.get("ok") is not True:
                raise WorkerRefusal("load_denied", "Hub bestaetigte die Freigabe nicht",
                                    hub_reason=str(answer.get("grund", "hub_invalid_schema")))
        except (OSError, WorkerRefusal) as exc:
            reason = (exc.details.get("hub_reason") if isinstance(exc, WorkerRefusal)
                      else "hub_unreachable")
            print(f"hub=gpu outcome=release_failed hub_reason={reason}",
                  file=sys.stderr, flush=True)
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

    def _error(self, status: int, reason: str, message: str, **details: object) -> None:
        body = json.dumps({"v": 1, "reason": reason, "message": message, **details},
                          separators=(",", ":")).encode()
        self._json(status, body)

    def _json(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in {"/synthesize", "/warm"}:
            self._error(400, "bad_request", "unbekannter Endpunkt")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            request = json.loads(self.rfile.read(length))
            if self.path == "/warm":
                status = ENGINE.request_warm(request)
                if status == 409:
                    self._error(409, "warm_in_progress", "Warmlauf laeuft bereits")
                else:
                    body = json.dumps({"v": 1, "status":
                                       "already_warm" if status == 200 else "accepted"},
                                      separators=(",", ":")).encode()
                    self._json(status, body)
                return
            if request.get("mode") not in REVISIONS or not isinstance(request.get("text"), str):
                raise ValueError
            job = ENGINE.submit(request)
        except (ValueError, json.JSONDecodeError, AttributeError):
            self._error(400, "bad_request", "ungueltige interne Anfrage")
            return
        except WorkerRefusal as exc:
            status = 503 if exc.reason in {"cold", "load_denied"} else (
                400 if exc.reason == "bad_request" else 429)
            self._error(status, exc.reason, exc.message, **exc.details)
            return
        # Der Kopf gehoert in dasselbe try wie der Stream: bricht die Gegenstelle
        # zwischen submit und end_headers weg, verliess die BrokenPipeError den
        # Handler ohne job.cancelled -- der Owner-Thread blieb dann fuer immer in
        # emit() haengen und der Worker nahm keinen Job mehr an, ohne eine Zeile
        # zu loggen.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.mimic.frames")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
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


def leerlauf_wache(server) -> threading.Event:
    """Setzt das Ereignis genau dann, wenn `handle_request` ohne Verbindung endet.

    `handle_request()` kehrt auch beim Verbindungseingang zurueck -- bei
    ThreadingMixIn sogar sofort, weil der Handler in einem eigenen Thread laeuft.
    Wer stattdessen die verstrichene Zeit misst, haelt eine Anfrage, die nach
    langer Ruhe eintrifft, faelschlich fuer Leerlauf: sie wird angenommen,
    dispatcht, und dann beendet die Schleife den Prozess. `daemon_threads` raeumt
    den Handler-Thread dabei wortlos weg -- der Aufrufer sah "Worker starb vor dem
    Stream", das Journal keinen Grund. Beobachtet am 2026-08-09, 290 s nach der
    vorigen Verbindung.
    """
    leerlauf = threading.Event()
    server.handle_timeout = leerlauf.set
    return leerlauf


def main() -> int:
    global ENGINE
    ENGINE = Engine()
    path = worker_socket_path()
    with _server(WorkerHandler, path) as server:
        server.timeout = IDLE_TIMEOUT
        leerlauf = leerlauf_wache(server)
        # Ein Timeout am horchenden Socket beendet main direkt. Das ist absichtlich
        # kein Timer-Thread: erst der Prozessausgang gibt RAM und VRAM sicher frei.
        while True:
            server.handle_request()
            if ENGINE.fatal.is_set():
                return 0
            if leerlauf.is_set():
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
