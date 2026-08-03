"""One process-scoped external Agentflow state home for the test suite."""

from __future__ import annotations

import os
import tempfile


_STATE_HOME = tempfile.TemporaryDirectory(prefix="agentflow-test-state-")
os.environ.setdefault("AGENTFLOW_STATE_HOME", _STATE_HOME.name)
