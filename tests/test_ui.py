"""Browser test of the whole customer journey (opt-in: needs Node.js and Playwright).

    AGENTGEN_UI_TEST=1 pytest tests/test_ui.py
Starts the real web app with the scripted model on a free port and drives it with
tests/ui_e2e.cjs; screenshots go to the pytest temp directory.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).parent

pytestmark = pytest.mark.skipif(os.environ.get("AGENTGEN_UI_TEST") != "1",
                                reason="set AGENTGEN_UI_TEST=1 to run the browser test")


def test_customer_journey_in_browser(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen([sys.executable, str(HERE / "e2e_server.py"), str(tmp_path / "ui.db"), str(port)],
                              cwd=HERE.parent)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        shots = tmp_path / "screenshots"
        shots.mkdir()
        result = subprocess.run(["node", str(HERE / "ui_e2e.cjs"), f"http://127.0.0.1:{port}", str(shots)],
                                capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "UI E2E PASSED" in result.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)
