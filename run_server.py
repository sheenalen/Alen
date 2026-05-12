from __future__ import annotations

import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "kanthari-server.log"
ERR_PATH = BASE_DIR / "kanthari-server.err.log"
APP_PATH = BASE_DIR / "app.py"


def main() -> None:
    python = sys.executable or "python"
    with LOG_PATH.open("ab") as stdout, ERR_PATH.open("ab") as stderr:
        subprocess.Popen(
            [python, "-u", str(APP_PATH)],
            cwd=BASE_DIR,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    print("Kanthari is starting on http://127.0.0.1:5000")


if __name__ == "__main__":
    main()
