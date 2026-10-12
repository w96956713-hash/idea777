"""Test-only launcher: the real web app with the scripted model (tests/fake_model.py).

Used by tests/ui_e2e.cjs to drive the UI in a browser without a model API key.
Never use this to demonstrate the product as if a real model were generating agents.
    python tests/e2e_server.py DB_PATH PORT
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn  # noqa: E402

from agentgen.web.app import create_app  # noqa: E402
from fake_model import ScriptedProvider  # noqa: E402

model = ScriptedProvider()
app = create_app(sys.argv[1], provider_factory=lambda: model, worker_poll_seconds=2)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
