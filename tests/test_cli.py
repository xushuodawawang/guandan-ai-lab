import os
import subprocess
import sys

import pytest

from guandan_ai.platform import ROOT


@pytest.mark.integration
def test_cli_chinese_output_with_legacy_redirected_encoding():
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run([sys.executable, "-m", "guandan_ai", "doctor"],
                            cwd=ROOT, env=environment, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "官方动作引擎正常" in result.stdout.decode("utf-8")
