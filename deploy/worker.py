"""Start the persistent single-worker service from a prepared runtime."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
storage_env = ROOT / "state" / "oss.env"
if storage_env.exists():
    for line in storage_env.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.startswith("#"):
            os.environ.setdefault(key, value)
os.environ.setdefault("CONFIG_PATH", str(ROOT / "state" / "config.local.yaml"))
os.environ.setdefault("RESULTS_ROOT", str(ROOT / "results"))
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(ROOT / ".token-cache"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001, workers=1, proxy_headers=False)
