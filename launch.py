#!/usr/bin/env python3
"""One-command Linux foreground launcher; no Django import before isolation.

@File: launch.py
@Brief: Private first-run environment and same-process manager/child ownership.
@Date: 03/10/2026
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import secrets
import signal
import subprocess
import sys
import threading
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# --- XDG state cannot become a source/project package member ---
def private_path(path: Path) -> Path:
    """Reject relative XDG paths and existing source/project package locations."""
    resolved = path.resolve()
    if (
        not path.is_absolute()
        or resolved.is_relative_to(ROOT)
        or any((parent / "project.json").exists() for parent in (resolved, *resolved.parents))
    ):
        raise RuntimeError("XDG paths must be absolute and outside source/project packages")
    return resolved


# --- First-run installation only; a successful repeat never invokes pip ---
def bootstrap(install: bool = False) -> None:
    """Create a checkout-specific private venv, then exec it in the foreground."""
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Linux with Python 3.12 is required: python3.12 launch.py")
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
    env = private_path(
        base / "local-survey-filter" / "envs" / hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    )
    os.environ["XDG_CACHE_HOME"] = str(base.resolve())
    env.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (env / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        python = env / "bin" / "python"
        marker = env / "requirements.sha256"
        digest = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
        if marker.exists() and marker.read_text() != digest and not install:
            raise RuntimeError("requirements.txt changed; deliberately run launch.py --install")
        if not python.exists():
            print(f"Creating isolated Python environment at {env}", flush=True)
            venv.EnvBuilder(with_pip=True).create(env)
        if not marker.exists() or install:
            print(
                "Installing existing requirements (internet or a populated pip cache required).",
                flush=True,
            )
            result = subprocess.run(
                [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    "dependency installation failed; check connectivity/cache and permissions, then retry"
                )
            marker.write_text(digest)
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import django, httpx, dotenv, plotly; assert django.VERSION[0] == 5",
            ],
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                "isolated dependencies missing/incompatible; run launch.py --install to repair"
            )
    if Path(sys.prefix) != env:
        arguments = [arg for arg in sys.argv[1:] if arg != "--install"]
        os.execv(str(python), [str(python), str(ROOT / "launch.py"), *arguments])


# --- Private manager configuration, never a legacy database fallback ---
def configure(legacy_env: str | None = None) -> Path:
    """Select XDG manager state and credentials before Django settings load."""
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).expanduser()
    state = private_path(base / "local-survey-filter")
    os.environ["XDG_DATA_HOME"] = str(state.parent)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    secret = state / "django-secret"
    try:
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48))
    credentials = state / "credentials.env"
    if legacy_env:
        credentials = Path(legacy_env).expanduser().resolve(strict=True)
        if not credentials.is_file():
            raise RuntimeError("--legacy-env must name an existing credential file")
    elif (ROOT / ".env").exists():
        print(
            "Existing source .env left untouched and not loaded; use --legacy-env /absolute/path/.env to opt in.",
            flush=True,
        )
    os.environ.update(
        WP_ROLE="manager",
        WP_LAUNCHER="1",
        DJANGO_SETTINGS_MODULE="config.settings",
        LOCAL_SURVEY_DB=str(state / "manager.sqlite3"),
        LOCAL_SURVEY_CREDENTIALS_FILE=str(credentials),
        DJANGO_SECRET_KEY=secret.read_text(),
    )
    os.environ.pop("WP_PROJECT_UUID", None)
    os.environ.pop("WP_WORKER_EPOCH", None)
    return state


# --- Non-daemon HTTP threads: server_close joins accepted edits, never kills ---
def serve(port: int, *, worker: bool = False) -> None:
    """Serve local static + Django, own workers here, and patiently drain on exit."""
    import django
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.servers.basehttp import ThreadedWSGIServer, WSGIRequestHandler
    from django.core.wsgi import get_wsgi_application

    from literature import workspace as ws
    from literature.project_store import ProjectStoreError, app_registry_path, init_app_registry
    from literature.worker import bound_store, set_draining

    django.setup()
    if not worker:
        try:
            init_app_registry(app_registry_path())
        except ProjectStoreError as exc:
            raise RuntimeError(f"private registry cannot initialise: {exc}") from exc
    if worker:
        binding = bound_store()
        if not binding["ok"]:
            raise RuntimeError(f"project worker binding refused: {binding['error']}")

    class Server(ThreadedWSGIServer):
        daemon_threads = False
        block_on_close = True

        def get_request(self):
            request, address = super().get_request()
            request.settimeout(30)  # Idle keep-alive clients must not strand shutdown.
            return request, address

    server = Server(("127.0.0.1", port), WSGIRequestHandler)
    server.set_app(StaticFilesHandler(get_wsgi_application()))
    stop = threading.Event()
    # ponytail: handlers only request shutdown; the main thread owns cleanup.
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    owner_read = owner_write = None
    if worker:
        owner_read = int(os.environ["WP_OWNER_FD"])

        def watch_parent() -> None:
            # A retained pipe, not a PID: EOF proves our actual supervisor died.
            try:
                while os.read(owner_read, 1):
                    pass
            finally:
                stop.set()

        threading.Thread(target=watch_parent, daemon=True).start()
    else:
        owner_read, owner_write = os.pipe()
        os.environ["WP_OWNER_FD"] = str(owner_read)
        os.environ["WP_MANAGER_BASE"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2})
    thread.start()
    print(
        f"{'Worker' if worker else 'Manager'} PID {os.getpid()} at http://127.0.0.1:{server.server_port}/ (Ctrl+C drains accepted edits)",
        flush=True,
    )
    try:
        while not stop.wait(1):
            if not worker:
                ws.sweep_idle()
    finally:
        if worker:
            set_draining(True)
        server.shutdown()
        thread.join()
        server.server_close()  # Includes accepted manager New/Load calls and transactions.
        if not worker:
            # No new manager request can spawn after this snapshot. Popen objects
            # are in THIS supervisor, never inferred from registry PIDs.
            for canonical, child in list(ws._CHILDREN.items()):
                print(f"Draining owned worker PID {child.pid}: {canonical}", flush=True)
                if child.poll() is None:
                    child.terminate()
            for canonical, child in list(ws._CHILDREN.items()):
                child.wait()  # ponytail: patient, no forced-kill timeout/data-loss ceiling.
                ws.forget_worker(canonical)
                ws._CHILDREN.pop(canonical, None)
            os.close(owner_write)
        os.close(owner_read)


# --- Public command plus internal fixed-worker entry (never bootstrap a worker) ---
def main() -> int:
    parser = argparse.ArgumentParser(description="Linux local survey manager; Python 3.12 required")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--install", action="store_true", help="explicitly repair/apply existing requirements"
    )
    parser.add_argument(
        "--legacy-env", help="explicit existing .env compatibility (settings saves to that file)"
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        if args.worker:
            if (
                os.environ.get("WP_ROLE") != "worker"
                or "WP_OWNER_FD" not in os.environ
                or "WP_WORKER_LOCK_FD" not in os.environ
            ):
                raise RuntimeError("internal worker needs a fixed binding and supervisor pipe")
            serve(args.port, worker=True)
        else:
            os.umask(0o077)
            bootstrap(args.install)
            state = configure(args.legacy_env)
            with (state / "launcher.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(
                        "a foreground manager already owns this XDG state; use its URL"
                    ) from exc
                serve(args.port)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        print(f"Launch failed: {exc}. No unrelated listener was stopped.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
