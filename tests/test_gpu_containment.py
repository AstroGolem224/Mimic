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
        self.assertIn(b"worker_unavailable", result["first"][1])
        status, _body = self.speak("Neuer Worker nach SIGKILL.")
        self.assertEqual(200, status)

    def test_09_memorymax_exhaustion_is_machine_readable(self):
        subprocess.run(["systemctl", "--user", "set-property", "--runtime",
                        "mimic-worker.service", "MemoryMax=1M"], check=True)
        try:
            subprocess.run(["systemctl", "--user", "stop", "mimic-worker.service"], check=True)
            status, body = self.speak("Speichergrenze provozieren.")
            self.assertIn(status, (503, 504))
            self.assertTrue(any(reason in body for reason in
                                (b"worker_unavailable", b"worker_timeout")))
            healthy = request("GET", "/status")
            self.assertEqual(200, healthy.status)
        finally:
            subprocess.run(["systemctl", "--user", "set-property", "--runtime",
                            "mimic-worker.service", "MemoryMax=7G"], check=True)
