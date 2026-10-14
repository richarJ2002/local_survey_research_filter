"""
Own isolated manager/worker startup and guarded Django dispatch.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File: launcher.py
@Brief: Private first-run environment and same-process manager/child ownership.
@Date: 03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import signal
import subprocess
import sys
import threading
import venv
from collections.abc import Sequence
from pathlib import Path
from socket import socket

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- XDG state cannot become a source/project package member ---
def private_path(path: Path) -> Path:
    """Reject relative XDG paths and source/project package locations.

    Args:
        path: Proposed private state or environment directory.

    Kwargs:
        None

    Returns:
        Path: Canonical absolute directory outside portable/source packages.

    Raises:
        RuntimeError: If the path is relative or inside a package.
    """
    resolved = path.resolve()
    if (
        not path.is_absolute()
        or resolved.is_relative_to(ROOT)
        or any((parent / "project.json").exists()
               for parent in (resolved, *resolved.parents))
    ):
        raise RuntimeError(
            "XDG paths must be absolute and outside source/project packages")
    return resolved


# --- First-run installation only; a successful repeat never invokes pip ---
def bootstrap(install: bool = False, *,
              arguments: Sequence[str] | None = None) -> None:
    """Create/check the private venv and replace this process when needed.

    Args:
        None

    Kwargs:
        install: Deliberately repair/apply requirements; defaults to False.
        arguments: Launcher arguments to retain across exec; defaults to
            sys.argv[1:]. The one-shot --install flag is removed.

    Returns:
        None

    Raises:
        RuntimeError: If the platform, requirements hash or dependencies fail.
        OSError: If private environment creation or exec fails.

    Side Effects:
        Serialize installation, write the successful hash and exec the venv.
    """
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            "Linux with Python 3.12 is required: python3.12 launch.py")
    base = Path(os.environ.get("XDG_CACHE_HOME",
                Path.home() / ".cache")).expanduser()
    env = private_path(
        base / "local-survey-filter" / "envs" /
        hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    )
    os.environ["XDG_CACHE_HOME"] = str(base.resolve())
    env.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (env / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        python = env / "bin" / "python"
        marker = env / "requirements.sha256"
        digest = hashlib.sha256(
            (ROOT / "requirements.txt").read_bytes()).hexdigest()
        if marker.exists() and marker.read_text() != digest and not install:
            raise RuntimeError(
                "requirements.txt changed; deliberately run "
                "launch.py --install")
        if not python.exists():
            print(f"Creating isolated Python environment at {env}", flush=True)
            venv.EnvBuilder(with_pip=True).create(env)
        if not marker.exists() or install:
            print(
                "Installing existing requirements "
                "(internet or a populated pip cache required).",
                flush=True,
            )
            result = subprocess.run(
                [str(python), "-m", "pip", "install",
                 "-r", str(ROOT / "requirements.txt")],
                check=False,
            )
            if result.returncode:
                raise RuntimeError(
                    "dependency installation failed; check connectivity/cache "
                    "and permissions, then retry"
                )
            marker.write_text(digest)
        result = subprocess.run(
            [
                str(python),
                "-c",
                ("import django, httpx, dotenv, plotly; "
                 "assert django.VERSION[0] == 5"),
            ],
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                "isolated dependencies missing/incompatible; "
                "run launch.py --install to repair"
            )
    if Path(sys.prefix) != env:
        arguments = [arg for arg in (
            sys.argv[1:] if arguments is None else arguments
        ) if arg != "--install"]
        os.execv(str(python), [str(python), str(
            ROOT / "launch.py"), *arguments])


# --- Private manager configuration, never a legacy database fallback ---
def configure(legacy_env: str | None = None) -> Path:
    """Select private manager state before loading Django settings.

    Args:
        None

    Kwargs:
        legacy_env: Explicit credential file compatibility; defaults to None.

    Returns:
        Path: Private manager state directory holding its startup lock.

    Raises:
        RuntimeError: If state is in a package or credentials are not a file.
        OSError: If state or secret access fails.

    Side Effects:
        Create private state/secret, set manager binding and credential path,
        and discard inherited worker identity. Never read the source .env.
    """
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() /
                ".local" / "share")).expanduser()
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
            raise RuntimeError(
                "--legacy-env must name an existing credential file")
    elif (ROOT / ".env").exists():
        print(
            "Existing source .env left untouched and not loaded; "
            "use --legacy-env /absolute/path/.env to opt in.",
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


# --- Non-daemon HTTP threads join accepted edits on server_close ---
def serve(port: int, *, worker: bool = False) -> None:
    """Serve loopback HTTP and drain admitted work and owned children on exit.

    Args:
        port: Loopback TCP port; zero requests an available port.

    Kwargs:
        worker: Serve an inherited fixed worker binding; defaults to False.

    Returns:
        None

    Raises:
        RuntimeError: If the registry or fixed worker binding is refused.
        OSError: If the listener, ownership pipe or shutdown operation fails.

    Side Effects:
        Load Django after isolation, install signal handlers and serve until
        signalled or the worker's retained supervisor pipe reaches EOF.
    """
    import django
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core.servers.basehttp import (
        ThreadedWSGIServer,
        WSGIRequestHandler,
    )
    from django.core.wsgi import get_wsgi_application

    from literature import workspace as ws
    from literature.project_store import (
        ProjectStoreError,
        app_registry_path,
        init_app_registry,
    )
    from literature.worker import bound_store, set_draining

    django.setup()
    if not worker:
        try:
            init_app_registry(app_registry_path())
        except ProjectStoreError as exc:
            raise RuntimeError(
                f"private registry cannot initialise: {exc}") from exc
    if worker:
        binding = bound_store()
        if not binding["ok"]:
            raise RuntimeError(
                f"project worker binding refused: {binding['error']}")

    class RequestHandler(WSGIRequestHandler):
        """Close expected idle connections without silencing request failures."""

        def handle_one_request(self) -> None:
            """Handle one request, quietly closing an expired request-line wait.

            Args:
                None

            Kwargs:
                None

            Returns:
                None

            Raises:
                TimeoutError: If a timeout occurs after a request line arrived.

            Side Effects:
                Close idle or incomplete-line connections. Other exceptions
                retain Django's normal error handling and reporting.
            """
            # A keep-alive wait must not retain the previous request's line.
            self.raw_requestline = b""
            try:
                super().handle_one_request()
            except TimeoutError:
                if self.raw_requestline:
                    raise
                self.close_connection = True

    class Server(ThreadedWSGIServer):
        """Join accepted request threads before releasing worker ownership."""

        daemon_threads = False
        block_on_close = True

        def get_request(self) -> tuple[socket, tuple[str, int]]:
            """Bound idle-client waits without interrupting admitted edits.

            Args:
                None

            Kwargs:
                None

            Returns:
                tuple: Accepted socket and peer address, with a 30s timeout.

            Raises:
                OSError: If accepting or configuring the socket fails.
            """
            request, address = super().get_request()
            # Idle keep-alive clients must not strand shutdown.
            request.settimeout(30)
            return request, address

    server = Server(("127.0.0.1", port), RequestHandler)
    server.set_app(StaticFilesHandler(get_wsgi_application()))
    stop = threading.Event()
    # ponytail: handlers only request shutdown; the main thread owns cleanup.

    def request_stop(*signals: object) -> None:
        """Request shutdown without running cleanup in a signal handler.

        Args:
            *signals: Signal number and frame supplied by signal.signal.

        Kwargs:
            None

        Returns:
            None
        """
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    owner_read = owner_write = None
    if worker:
        owner_read = int(os.environ["WP_OWNER_FD"])

        def watch_parent() -> None:
            """Request a patient drain when the retained owner pipe closes.

            Args:
                None

            Kwargs:
                None

            Returns:
                None

            Raises:
                OSError: If the ownership pipe cannot be read.
            """
            # A retained pipe, not a PID: EOF proves our actual supervisor
            # died.
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
    thread = threading.Thread(target=server.serve_forever, kwargs={
                              "poll_interval": 0.2})
    thread.start()
    print(
        f"{'Worker' if worker else 'Manager'} PID {os.getpid()} "
        f"at http://127.0.0.1:{server.server_port}/ "
        "(Ctrl+C drains accepted edits)",
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
        # Includes accepted manager New/Load calls and transactions.
        server.server_close()
        if not worker:
            # No new manager request can spawn after this snapshot. Popen
            # objects are in THIS supervisor, not inferred from registry PIDs.
            for canonical, child in list(ws._CHILDREN.items()):
                print(
                    f"Draining owned worker PID {child.pid}: {canonical}",
                    flush=True)
                if child.poll() is None:
                    child.terminate()
            for canonical, child in list(ws._CHILDREN.items()):
                # ponytail: patient, no forced-kill timeout/data-loss ceiling.
                child.wait()
                ws.forget_worker(canonical)
                ws._CHILDREN.pop(canonical, None)
            os.close(owner_write)
        os.close(owner_read)


# --- Fixed startup roles; never bootstrap a worker or developer command ---
def run_manager(port: int = 8000, *, install: bool = False,
                legacy_env: str | None = None, worker: bool = False,
                arguments: Sequence[str] | None = None) -> int:
    """Start the foreground manager or its internally supervised worker.

    Args:
        None

    Kwargs:
        port: Loopback TCP port, defaults to 8000; zero selects a free port.
        install: Explicit dependency repair, defaults to False.
        legacy_env: Explicit credential compatibility file, defaults to None.
        worker: Internal worker path without bootstrap, defaults to False.
        arguments: Arguments retained across bootstrap exec; defaults to
            sys.argv[1:].

    Returns:
        int: Zero after clean drain, 130 on interrupt, or 1 on startup failure.

    Side Effects:
        Own the private manager lock and report bounded startup failures.
    """
    try:
        if not 0 <= port <= 65535:
            raise RuntimeError("port must be between 0 and 65535")
        if worker:
            if (
                os.environ.get("WP_ROLE") != "worker"
                or "WP_OWNER_FD" not in os.environ
                or "WP_WORKER_LOCK_FD" not in os.environ
            ):
                raise RuntimeError(
                    "internal worker needs a fixed binding and supervisor "
                    "pipe")
            serve(port, worker=True)
        else:
            os.umask(0o077)
            bootstrap(install, arguments=arguments)
            state = configure(legacy_env)
            with (state / "launcher.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(
                        "a foreground manager already owns this XDG state; "
                        "use its URL"
                    ) from exc
                serve(port)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ImportError,
            subprocess.SubprocessError) as exc:
        print(
            f"Launch failed: {exc}. No unrelated listener was stopped.",
            file=sys.stderr)
        return 1
    return 0


# --- Guard even unknown commands before Django/dotenv/database access ---
def run_django(arguments: Sequence[str]) -> int:
    """Dispatch Django in the current interpreter with an explicit DB guard.

    Args:
        arguments: Unparsed Django command arguments, including help flags.

    Kwargs:
        None

    Returns:
        int: Zero on success or 2 when the DB/role guard refuses dispatch.

    Raises:
        ImportError: If this developer interpreter does not contain Django.
        SystemExit: If Django handles help, rejects arguments or fails.

    Side Effects:
        Set Django's settings module and an explicitly requested legacy opt-in
        before dispatch. Do not bootstrap manager dependencies or private
        state.
        Untargeted safe commands retain settings' in-memory database default.
    """
    arguments = list(arguments)
    legacy = "--legacy-shared-db" in arguments
    if legacy:
        arguments = [arg for arg in arguments if arg != "--legacy-shared-db"]
        if os.environ.get("WP_ROLE", "developer") != "developer":
            print("--legacy-shared-db is only for developer/rollback mode",
                  file=sys.stderr)
            return 2
    if (
        arguments
        and arguments[0] not in {
            "check", "test", "makemigrations", "help", "version"
        }
        and not any(arg in {"-h", "--help", "--version"}
                    for arg in arguments)
        and not os.environ.get("LOCAL_SURVEY_DB")
        and not legacy
    ):
        print(
            "administrative commands require an explicit LOCAL_SURVEY_DB or "
            "--legacy-shared-db; refusing the implicit projects/db.sqlite3 "
            "target. Start the New / Load manager with python3.12 launch.py.",
            file=sys.stderr,
        )
        return 2
    if legacy:
        os.environ["LOCAL_SURVEY_LEGACY_SHARED_DB"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line([str(ROOT / "launch.py"), *arguments])
    return 0
