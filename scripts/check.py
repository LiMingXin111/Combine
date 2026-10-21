"""Run offline suites in separate processes so module-level test fixtures cannot leak."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = {**os.environ, "COMBINE_IGNORE_DOTENV": "1", "PYTHONIOENCODING": "utf-8", "WEB_SEARCH_MODE": "fake", "WECHAT_MODE": "off"}
for module in ("test_units", "test_websearch", "test_agents", "test_e2e", "test_reliability", "test_deployment", "test_team_spaces"):
    result = subprocess.run([sys.executable, "-m", "unittest", f"tests.{module}", "-q"], cwd=ROOT, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)
raise SystemExit(subprocess.run([sys.executable, "scripts/i18n_check.py"], cwd=ROOT, env=env).returncode)
