import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmarks/run_v1_full_frozen_20260921.py"
spec = importlib.util.spec_from_file_location("v1_frozen", SOURCE)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
module.BASE_URL = "http://47.96.153.159:8010/v1"
sys.argv = [
    str(SOURCE),
    "--model", "dashscope/glm-5.1",
    "--output", str(ROOT / "benchmarks/results/v1-glm51-public-20260928"),
    "--workers", "16",
    "--request-retries", "5",
    "--request-interval-seconds", "1.5",
]
module.main()
