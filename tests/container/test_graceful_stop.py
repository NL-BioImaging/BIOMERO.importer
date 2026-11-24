# Copyright (c) 2026, NL-BioImaging. GNU GPL version 2.
"""Exercise an image's unchanged entrypoint with a real Docker stop signal."""

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

FIXTURES = Path(__file__).resolve().parent / "fixtures"
READY_TIMEOUT_SECONDS = 30
STOP_TIMEOUT_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 30


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a bounded Docker command, returning captured diagnostics."""
    return subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, check=check,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


class ContainerStopTests(unittest.TestCase):
    """Verify signal delivery through the built image's default entrypoint."""

    image: str

    def test_default_entrypoint_delivers_sigterm_and_exits_zero(self) -> None:
        name = f"importer-signal-test-{uuid4().hex}"
        docker("create", "--name", name, "--network", "none", "--workdir",
               "/signal-probe", self.image)
        # Cleanup touches only the uniquely named container this test created.
        self.addCleanup(docker, "rm", "--force", "--volumes", name)
        # A shadow module avoids all OMERO/database connections. Keep the real
        # interpreter, runtime libraries and image entrypoint unchanged.
        docker("cp", f"{FIXTURES}/.", f"{name}:/signal-probe")
        docker("start", name)
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            state = json.loads(docker("inspect", name).stdout)[0]["State"]
            if not state["Running"]:
                logs = docker("logs", name)
                self.fail(f"Signal receiver exited: {state}; {logs.stdout}{logs.stderr}")
            if docker("exec", name, "test", "-f", "/tmp/importer-signal-ready",
                      check=False).returncode == 0:
                break
            time.sleep(0.2)
        else:
            self.fail("Signal receiver did not become ready")

        docker("stop", "--time", str(STOP_TIMEOUT_SECONDS), name)
        state = json.loads(docker("inspect", name).stdout)[0]["State"]
        self.assertFalse(state["Running"])
        self.assertEqual(state["ExitCode"], 0, "Entry point failed to stop cleanly")
        self.assertFalse(state["OOMKilled"])
        with tempfile.TemporaryDirectory(prefix="importer-signal-result-") as directory:
            result = Path(directory) / "signal"
            docker("cp", f"{name}:/tmp/importer-stop-signal", str(result))
            self.assertEqual(int(result.read_text(encoding="utf-8")), signal.SIGTERM)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Built importer image to test")
    ContainerStopTests.image = parser.parse_args().image
    unittest.main(argv=[sys.argv[0]], verbosity=2)
