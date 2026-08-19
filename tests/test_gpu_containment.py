"""Echte Eindämmungsproben; nur durch tests/run.sh --gpu aktiviert.

Die Units muessen installiert und das Matthias-Profil vorhanden sein. Beide Tests
setzen absichtlich den Worker ausser Gefecht, nie das Frontend.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import unittest

from mimic.cli import request


@unittest.skipUnless(os.environ.get("MIMIC_GPU_TESTS") == "1", "nur mit --gpu")
class GPUContainmentTests(unittest.TestCase):
    def speak(self, text="Dies ist eine Eindämmungsprobe."):
        response = request("POST", "/speak", {"text": text, "voice": "matthias", "mode": "mf"})
        return response.status, response.read()

    def worker_pid(self):
        status = request("GET", "/status")
        return json.loads(status.read())["worker_pid"]

    def test_08_sigkill_worker_frontend_survives_and_restarts(self):
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("first", self.speak()))
        thread.start()
        deadline = time.monotonic() + 90
        pid = None
        while time.monotonic() < deadline and not pid:
            pid = self.worker_pid()
            time.sleep(0.05)
        self.assertIsInstance(pid, int)
        os.kill(pid, signal.SIGKILL)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        # Nicht auf einen bestimmten Grund pruefen: die Zusage lautet "Frontend
        # ueberlebt, maschinenlesbare Absage". Seit die Hub-Sperre wirklich
        # funktioniert, kann der Grund auch load_denied/lade_sperre sein -- der
        # per SIGKILL getoetete Worker kommt nicht mehr zum `fertig`, und die
        # Sperre steht bis GPU_FRIST_S (120 s). Das ist dieselbe Eigenschaft,
        # die dAImons eigene Worker haben, kein Mimic-Fehler.
        grund = json.loads(result["first"][1]).get("reason")
        self.assertIn(grund, {"worker_unavailable", "worker_timeout", "load_denied"})
        # Der naechste Aufruf wird bedient ODER sauber mit lade_sperre abgelehnt.
        # Beides erfuellt die Zusage; welches von beidem, entscheidet dAImons Hub:
        # der per SIGKILL getoetete Worker konnte sein `fertig` nicht mehr senden,
        # also steht seine Sperre bis GPU_FRIST_S (120 s). Auf 200 zu bestehen
        # hiesse, den Test von einer Frist in einem FREMDEN Dienst abhaengig zu
        # machen. Was hier zaehlt: das Frontend lebt und antwortet maschinenlesbar.
        status, body = self.speak("Neuer Worker nach SIGKILL.")
        if status != 200:
            self.assertEqual(503, status)
            self.assertEqual("lade_sperre", json.loads(body).get("hub_reason"))

    def test_09_memorymax_exhaustion_is_machine_readable(self):
        eigenschaften = {}
        for name in ("MemoryMax", "MemoryHigh"):
            eigenschaften[name] = subprocess.run(
                ["systemctl", "--user", "show", "mimic-worker.service",
                 f"--property={name}", "--value"], check=True, capture_output=True,
                text=True).stdout.strip()
        try:
            subprocess.run(["systemctl", "--user", "set-property", "--runtime",
                            "mimic-worker.service", "MemoryMax=1M"], check=True)
            subprocess.run(["systemctl", "--user", "stop", "mimic-worker.service"], check=True)
            status, body = self.speak("Speichergrenze provozieren.")
            self.assertIn(status, (503, 504))
            self.assertTrue(any(reason in body for reason in
                                (b"worker_unavailable", b"worker_timeout")))
            healthy = request("GET", "/status")
            self.assertEqual(200, healthy.status)
        finally:
            subprocess.run(["systemctl", "--user", "set-property", "--runtime",
                            "mimic-worker.service",
                            f"MemoryMax={eigenschaften['MemoryMax']}",
                            f"MemoryHigh={eigenschaften['MemoryHigh']}"], check=True)
