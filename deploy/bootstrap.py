"""Sync the worker's Python runtime from uv.lock inside its container."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
runtime = root / ".runtime"
env = {
    **os.environ,
    "UV_PROJECT_ENVIRONMENT": str(runtime),
    "UV_CACHE_DIR": str(root / ".bootstrap" / "uv-cache"),
    "UV_PYTHON_DOWNLOADS": "never",
    "UV_LINK_MODE": "copy",
    "TIKTOKEN_CACHE_DIR": str(root / ".token-cache"),
}
subprocess.run(
    ["uv", "sync", "--locked", "--no-dev", "--group", "media", "--no-install-project", "--python", sys.executable],
    cwd=root, env=env, check=True,
)
python = str(runtime / "bin" / "python")
subprocess.run(["uv", "pip", "check", "--python", python], cwd=root, env=env, check=True)
subprocess.run(
    [python, "-c", "import tiktoken; tiktoken.get_encoding('o200k_base')"],
    cwd=root, env=env, check=True,
)
