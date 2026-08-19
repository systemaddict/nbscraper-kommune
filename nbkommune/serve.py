"""Run FastAPI on loopback behind the Better Auth gateway."""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

from nbkommune.settings import Settings

logger = logging.getLogger(__name__)


def run_protected_server(settings: Settings, *, host: str, port: int, verbose: bool) -> int:
    """Supervise the internal API and the public Node authentication gateway."""
    settings.validate_auth()
    auth_entrypoint = Path(__file__).resolve().parents[1] / "auth" / "dist" / "server.js"
    if not auth_entrypoint.is_file():
        raise RuntimeError(
            f"Better Auth gateway is not built ({auth_entrypoint}); run `npm run build` in auth/"
        )
    if settings.auth_internal_port == port:
        raise ValueError("NBK_AUTH_INTERNAL_PORT must differ from the public dashboard port")

    log_level = "debug" if verbose else "info"
    upstream = f"http://127.0.0.1:{settings.auth_internal_port}"
    commands = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "nbkommune.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(settings.auth_internal_port),
            "--log-level",
            log_level,
        ],
        [
            "node",
            str(auth_entrypoint),
            "--host",
            host,
            "--port",
            str(port),
            "--upstream",
            upstream,
        ],
    ]
    children = [subprocess.Popen(command) for command in commands]

    def stop_children(_signum: int, _frame: object) -> None:
        for child in children:
            if child.poll() is None:
                child.terminate()

    previous = {
        signum: signal.signal(signum, stop_children)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        while True:
            for child in children:
                exit_code = child.poll()
                if exit_code is not None:
                    logger.info("dashboard child exited with status %s", exit_code)
                    stop_children(0, None)
                    for sibling in children:
                        if sibling is not child:
                            sibling.wait(timeout=10)
                    return exit_code
            time.sleep(0.2)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
