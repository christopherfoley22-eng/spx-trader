"""Run the browser-script timing regression as part of the offline suite."""

from pathlib import Path
import subprocess

script = Path(__file__).with_suffix(".js")
subprocess.run(["node", str(script)], check=True, timeout=15)
