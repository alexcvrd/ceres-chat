"""Smoke test: imports, the page is served, and the offline test suite passes. No Ceres needed."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(HERE / "tests")], cwd=HERE))
