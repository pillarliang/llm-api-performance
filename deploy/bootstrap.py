"""Prepare the worker's Python runtime inside its standard Python container."""
from pathlib import Path
import subprocess
import venv

runtime = Path(".runtime")
if not (runtime / "bin" / "python").exists():
    venv.create(runtime, with_pip=True)
python = str(runtime / "bin" / "python")
subprocess.run([python, "-m", "pip", "install", "-r", "requirements.lock"], check=True)
subprocess.run([python, "-m", "pip", "check"], check=True)
subprocess.run([python, "-c", "import tiktoken; tiktoken.get_encoding('o200k_base')"], check=True)
