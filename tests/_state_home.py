"""One process-scoped external Agentflow state home for the test suite."""

from __future__ import annotations

import os
import tempfile


_STATE_HOME = tempfile.TemporaryDirectory(prefix="agentflow-test-state-")
# Keep the suite's process-scoped default on the compatibility path. Tests
# that exercise AGENTFLOW_STATE_HOME explicitly can then prove its precedence
# over this XDG fallback without sharing state with the host.
os.environ.setdefault("XDG_STATE_HOME", _STATE_HOME.name)
