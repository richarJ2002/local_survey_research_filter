"""Functional launcher qualification with synthetic checkout/state only.

@File: test_wp0110_launcher.py
@Brief: First install, offline repeat, owned drain/crash recovery and port failures.
@Date: 03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

# Custom library Imports
from django.test import SimpleTestCase, override_settings

from config import launcher
from literature.tests.test_wp0105_lifecycle import _Client

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

# --- Reject all non-loopback sockets in repeated runs, including child processes ---
OFFLINE = """
import socket
connect = socket.socket.connect
def local_only(self, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', 'localhost', '::1'):
        raise OSError('synthetic offline network')
    return connect(self, address)
socket.socket.connect = local_only
socket.socket.connect_ex = lambda self, address: (local_only(self, address) or 0)
"""


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Functions
def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# --- Entry dispatch must happen before Django/legacy state is touched ---
class StartupDispatchTests(SimpleTestCase):
    """Keep manager isolation and fixed Django bindings on separate paths."""

    def test_default_and_loopback_ports_use_the_same_runtime(self):
        """Preserve manager port selection without entering Django dispatch.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for arguments, port in (
            ([], 8000),
            (["--port", "8001"], 8001),
            (["--port", "8002"], 8002),
            (["--port", "0"], 0),
        ):
            with (
                self.subTest(arguments=arguments),
                tempfile.TemporaryDirectory(prefix="wp10-dispatch-") as tmp,
                patch.dict(os.environ, {}, clear=True),
                patch.object(launcher.os, "umask"),
                patch.object(launcher, "bootstrap") as bootstrap,
                patch.object(launcher, "configure", return_value=Path(tmp)),
                patch.object(launcher, "serve") as serve,
                patch("django.core.management.execute_from_command_line")
                as django_command,
            ):
                self.assertEqual(launcher.run_manager(
                    port, arguments=arguments), 0)
                bootstrap.assert_called_once_with(False, arguments=arguments)
                serve.assert_called_once_with(port)
                django_command.assert_not_called()

    def test_fixed_roles_and_explicit_legacy_keep_django_arguments(self):
        """Forward fixed-role and explicit rollback flags without bootstrap.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for role, legacy in (
            ("worker", False), ("manager", False), ("developer", True),
        ):
            arguments = ["runserver", "127.0.0.1:8001", "--noreload"]
            with (
                self.subTest(role=role),
                patch.dict(
                    os.environ, {"WP_ROLE": role,
                                 "LOCAL_SURVEY_DB": ":memory:"},
                    clear=True,
                ),
                patch.object(launcher, "bootstrap") as bootstrap,
                patch.object(launcher, "configure") as configure,
                patch("django.core.management.execute_from_command_line")
                as django_command,
            ):
                self.assertEqual(launcher.run_django(
                    arguments + (["--legacy-shared-db"] if legacy else [])), 0)
                django_command.assert_called_once_with(
                    [str(launcher.ROOT / "launch.py"), *arguments])
                bootstrap.assert_not_called()
                configure.assert_not_called()


class LauncherPreflightTests(SimpleTestCase):
    """Reject incompatible environments before opening application state."""

    def test_python_and_dependency_errors(self):
        """Expose Python, installation and dependency-probe failures.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with (
            patch.object(sys, "version_info", (3, 11)),
            self.assertRaisesRegex(RuntimeError, "3.12"),
        ):
            launcher.bootstrap()
        with (
            tempfile.TemporaryDirectory(prefix="wp10-preflight-") as tmp,
            patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}),
            patch("config.launcher.venv.EnvBuilder") as builder,
            patch("config.launcher.subprocess.run") as run,
        ):
            run.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, "installation failed"):
                launcher.bootstrap()
            builder.return_value.create.assert_called_once()
            env = next((Path(tmp) / "local-survey-filter" / "envs").iterdir())
            (env / "requirements.sha256").write_text(
                hashlib.sha256(
                    (launcher.ROOT / "requirements.txt").read_bytes()
                ).hexdigest()
            )
            with self.assertRaisesRegex(RuntimeError, "dependencies missing"):
                launcher.bootstrap()
            self.assertIn("import django", run.call_args.args[0][2])

    def test_private_path_and_permission_errors(self):
        """Refuse package state and surface private-directory permissions.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for location in (Path("relative"), launcher.ROOT / "private"):
            with self.assertRaisesRegex(RuntimeError, "outside source"):
                launcher.private_path(location)
        with tempfile.TemporaryDirectory(prefix="wp10-permission-") as tmp:
            root = Path(tmp)
            (root / "project.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "project packages"):
                launcher.private_path(root / "private")
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "state")}),
                patch("config.launcher.private_path",
                      return_value=root / "state"),
                patch(
                    "pathlib.Path.mkdir",
                    side_effect=PermissionError("synthetic permission denied"),
                ),
                self.assertRaises(PermissionError),
            ):
                launcher.configure()

    def test_settings_private_write_failure_is_visible(self):
        with tempfile.TemporaryDirectory(prefix="wp10-credentials-") as tmp:
            file = Path(tmp) / "credentials.env"
            with (
                override_settings(LOCAL_SURVEY_CREDENTIALS_FILE=file),
                patch("literature.views.set_key",
                      side_effect=PermissionError("synthetic denied")),
            ):
                response = self.client.post(
                    "/settings/", {"openalex_email": "synthetic@example.org"}
                )
            self.assertContains(response, "Cannot save the credential file")


class LauncherIntegrationTests(SimpleTestCase):
    """One real first-run pip install, then only network-disabled repeat launches."""

    @classmethod
    def setUpClass(cls):
        """Install once in a copied source checkout and isolated XDG state.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory(prefix="wp10-launch-")
        cls.root = Path(cls.tmp.name)
        cls.clone = cls.root / "source checkout with spaces"
        cls.clone.mkdir()
        # Git enumerates source only; copy the CURRENT dirty files, not HEAD.
        names = (
            subprocess.check_output(
                ["git", "ls-files", "--cached", "--others",
                 "--exclude-standard", "-z"],
                cwd=launcher.ROOT,
            )
            .decode()
            .split("\0")
        )
        for name in names:
            path = Path(name)
            if (
                not name
                or not (launcher.ROOT / path).is_file()
                or path.parts[0] in {
                    "projects", ".agents", "graphify-out", ".venv"
                }
                or (path.name.startswith(".env")
                    and path.name != ".env.example")
            ):
                continue
            if path.suffix not in {
                ".py",
                ".html",
                ".js",
                ".cjs",
                ".css",
                ".toml",
                ".txt",
                ".md",
            } and name not in {"LICENSE", ".gitignore", ".env.example"}:
                continue
            target = cls.clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(launcher.ROOT / path, target)
        cls.offline = cls.root / "offline"
        cls.offline.mkdir()
        (cls.offline / "sitecustomize.py").write_text(OFFLINE)
        cls.cache = cls.root / "cache with spaces"
        env = cls.environment(cls.root / "first-run", offline=False)
        log = cls.root / "first-run.log"
        with log.open("w") as stream:
            proc = subprocess.Popen(
                [sys.executable, str(cls.clone / "launch.py"), "--port", "0"],
                env=env,
                stdout=stream,
                stderr=stream,
            )
        try:
            assert wait_for(
                lambda: "Manager PID" in log.read_text() or proc.poll() is not None, 180
            ), "first-run did not start"
            assert proc.poll() is None, log.read_text()[-4000:]
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            proc.wait(timeout=40)
        cls.first_log = log.read_text()
        cls.environment_path = next(
            (cls.cache / "local-survey-filter" / "envs").iterdir())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    @classmethod
    def environment(cls, state, offline=True):
        env = dict(os.environ)
        for key in (
            "OPENALEX_EMAIL",
            "SEMANTIC_SCHOLAR_KEY",
            "OPENCITATIONS_TOKEN",
            "ELSEVIER_API_KEY",
            "ELSEVIER_INSTTOKEN",
            "DJANGO_SECRET_KEY",
            "WP_ROLE",
            "WP_PROJECT_UUID",
            "WP_WORKER_EPOCH",
            "WP_LAUNCHER",
            "WP_OWNER_FD",
            "LOCAL_SURVEY_CREDENTIALS_FILE",
            "LOCAL_SURVEY_LEGACY_SHARED_DB",
        ):
            env.pop(key, None)
        env.update(
            XDG_DATA_HOME=str(state),
            XDG_CACHE_HOME=str(cls.cache),
            PYTHON_DOTENV_DISABLED="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONUNBUFFERED="1",
        )
        if offline:
            env.update(PYTHONPATH=str(cls.offline), PIP_NO_INDEX="1")
        return env

    def setUp(self):
        self.work = self.root / self._testMethodName
        self.work.mkdir()
        self.env = self.environment(self.work / "state")
        self.log = self.work / "manager.log"
        self.proc = None
        self.master = None

    def tearDown(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait(timeout=40)
        if self.master is not None:
            os.close(self.master)

    def start(self, extra=(), terminal=False):
        """Start the copied manager, optionally in a real synthetic terminal.

        Args:
            None

        Kwargs:
            extra: Additional startup flags; defaults to ().
            terminal: Attach a controlling terminal for Ctrl+C; defaults False.

        Returns:
            None
        """
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        command = [sys.executable, str(self.clone / "launch.py"),
                   "--port", str(self.port), *extra]
        options = {}
        slave = None
        if terminal:
            self.master, slave = pty.openpty()
            options = {
                "stdin": slave,
                "start_new_session": True,
                "preexec_fn": lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
            }
        with self.log.open("a") as stream:
            self.proc = subprocess.Popen(
                command, env=self.env, stdout=stream, stderr=stream, **options
            )
        if slave is not None:
            os.close(slave)
        self.base = f"http://127.0.0.1:{self.port}"
        self.assertTrue(
            wait_for(
                lambda: (
                    f"http://127.0.0.1:{self.port}/" in self.log.read_text()
                    or self.proc.poll() is not None
                )
            ),
            self.log.read_text(),
        )
        self.assertIsNone(self.proc.poll(), self.log.read_text())
        self.manager = _Client()
        self.assertEqual(self.manager.get(self.base + "/")[0], 200)
        # _Client normally picks worker cookies; use this manager's own token.
        self.manager._csrf = lambda: next(
            c.value for c in self.manager.jar if c.name == "mgrcsrftoken"
        )

    def create(self):
        """Confirm a signed New preview and attach to its real project worker.

        Args:
            None

        Kwargs:
            None

        Returns:
            tuple: Store path, PID, port, epoch, handle and manifest.
        """
        directory = self.work / "project with spaces"
        fields = {"directory": str(directory), "name": "Synthetic launcher"}
        status, preview, _ = self.manager.post(
            self.base + "/workspace/new/", fields)
        self.assertEqual(status, 200, preview[:1000])
        self.assertFalse(directory.exists())
        token = re.search(rb'name="preview_token" value="([^"]+)"', preview)
        self.assertIsNotNone(token, preview[:1000])
        status, body, _ = self.manager.post(
            self.base + "/workspace/new/",
            fields | {"confirm": "on", "preview_token": token[1].decode()},
        )
        self.assertEqual(status, 200, body[:1000])
        registry = (
            self.work / "state" / "local-survey-filter" / "registry.sqlite3")
        with sqlite3.connect(registry) as connection:
            pid, port, epoch = connection.execute(
                "SELECT pid, port, worker_epoch FROM workers"
            ).fetchone()
        manifest = json.loads((directory / "project.json").read_text())
        client = _Client()
        base = (f"http://127.0.0.1:{port}/p/{manifest['project_uuid']}/{epoch}"
                "/project/1/lifecycle/")
        self.assertEqual(client.get(base + "drain/")[0], 200)
        status, body, _ = client.post(base + "attach/", {"nav": "navigate"})
        self.assertEqual(status, 200, body)
        handle = json.loads(body)["handle_id"]
        return directory, pid, port, epoch, handle, manifest

    def test_first_install_offline_repeat_static_and_credentials(self):
        self.assertEqual(self.first_log.count(
            "Installing existing requirements"), 1)
        self.start()
        status, body, _ = self.manager.get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"New/Load and local editing need no email", body)
        self.assertIn(b"Load", body)
        source_env = self.clone / ".env"
        source_env.write_text("OPENALEX_EMAIL=legacy-synthetic@example.org\n")
        self.addCleanup(source_env.unlink)
        self.assertEqual(self.manager.get(self.base + "/settings/")[0], 200)
        self.assertEqual(
            self.manager.post(
                self.base +
                "/settings/", {"openalex_email": "new-synthetic@example.org"}
            )[0],
            200,
        )
        credentials = self.work / "state" / "local-survey-filter" / "credentials.env"
        original = credentials.read_bytes()
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"new-synthetic", original)
        self.assertIn("legacy-synthetic", source_env.read_text())
        directory, _pid, port, epoch, handle, manifest = self.create()
        prefix = f"http://127.0.0.1:{port}/p/{manifest['project_uuid']}/{epoch}/project"
        worker = _Client(handle)
        self.assertEqual(worker.get(prefix + "/1/papers/")[0], 200)
        self.assertEqual(worker.get(prefix + "/plotly.min.js")[0], 200)
        self.assertEqual(
            worker.get(
                f"http://127.0.0.1:{port}/static/literature/project_handle.js")[0], 200
        )
        self.assertEqual(
            self.manager.post(self.base + "/workspace/open/",
                              {"directory": str(directory)})[0], 200
        )
        registry = self.work / "state" / "local-survey-filter" / "registry.sqlite3"
        with sqlite3.connect(registry) as connection:
            self.assertEqual(
                connection.execute("SELECT pid, port FROM workers").fetchall(), [
                    (_pid, port)]
            )
        self.proc.send_signal(signal.SIGINT)
        self.assertEqual(self.proc.wait(timeout=40), 0)
        self.assertEqual(worker.get(prefix + "/1/papers/")[0], "REFUSED")
        self.assertFalse((self.clone / "projects").exists())
        # Read only our newly saved synthetic credentials.
        self.env.pop("PYTHON_DOTENV_DISABLED")
        self.start()
        self.assertEqual(credentials.read_bytes(), original)
        settings_page = self.manager.get(self.base + "/settings/")[1]
        self.assertIn(b"new-synthetic@example.org", settings_page)
        self.assertNotIn(b"legacy-synthetic@example.org", settings_page)
        self.assertNotIn("Installing existing requirements",
                         self.log.read_text())
        # Explicit Load only; source-store byte content survives re-opening.
        before = (directory / "project.sqlite3").read_bytes()
        self.assertEqual(
            self.manager.post(self.base + "/workspace/open/",
                              {"directory": str(directory)})[0], 200
        )
        self.assertEqual((directory / "project.sqlite3").read_bytes(), before)

    def test_periodic_sweep_without_landing_visits(self):
        self.env.update(WP_HANDLE_TTL="1", WP_WORKER_GRACE="1")
        self.start()
        directory, _pid, port, _epoch, _handle, _manifest = self.create()
        registry = self.work / "state" / "local-survey-filter" / "registry.sqlite3"

        def closed():
            with sqlite3.connect(registry) as connection:
                return connection.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0

        self.assertTrue(wait_for(closed, 20))
        self.assertEqual(_Client().get(
            f"http://127.0.0.1:{port}/")[0], "REFUSED")
        self.assertTrue((directory / "project.sqlite3").exists())

    def test_source_only_synthetic_example_load_edit_share_roundtrip_offline(
        self,
    ):
        """Keep offline source packaging, local edits and copies lossless.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.tests.test_wp0108_copy_recovery import CopyRecoveryTests

        for name in (
            ".gitignore",
            ".env.example",
            "launch.py",
            "config/launcher.py",
            "LEGACY_CONVERSION.md",
            "RELEASE_NOTES.md",
            "THIRD_PARTY_NOTICES.md",
            "literature/project_store.py",
            "literature/migrations/0012_surveyentry_inclusion_origin.py",
            "literature/static/literature/graph_node_popup.js",
            "literature/tests/test_papers_table.py",
            "literature/tests/synthetic_example.py",
        ):
            self.assertEqual((self.clone / name).read_bytes(),
                             (launcher.ROOT / name).read_bytes())
        for name in (".env", "AGENTS.md", ".agents", "projects", "graphify-out",
                     "artifacts", "manage.py"):
            self.assertFalse((self.clone / name).exists())
        sample = self.work / "shared synthetic example"
        command = [
            str(self.environment_path / "bin" / "python"),
            "-m",
            "literature.tests.synthetic_example",
            str(sample),
        ]
        result = subprocess.run(
            command,
            cwd=self.clone,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ready for Load", result.stdout)
        self.assertEqual({p.name for p in sample.iterdir()},
                         {"project.json", "project.sqlite3"})
        original = CopyRecoveryTests.rows(sample)
        self.assertEqual(len(original["literature_paper"]), 3)
        self.assertEqual(len(original["literature_surveycitation"]), 3)
        repeat = subprocess.run(
            command,
            cwd=self.clone,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(repeat.returncode, 0)
        self.assertIn("absent absolute folder", repeat.stderr)
        self.assertEqual(CopyRecoveryTests.rows(sample), original)
        self.start()
        status, body, _ = self.manager.post(
            self.base + "/workspace/open/", {"directory": str(sample)}
        )
        self.assertEqual(status, 200, body[:1000])
        registry = (
            self.work / "state" / "local-survey-filter" / "registry.sqlite3")
        with sqlite3.connect(registry) as connection:
            port, epoch = connection.execute(
                "SELECT port, worker_epoch FROM workers").fetchone()
        identity = json.loads(
            (sample / "project.json").read_text())["project_uuid"]
        base = f"http://127.0.0.1:{port}/p/{identity}/{epoch}/project/1/"
        worker = _Client()
        worker.get(base)
        status, body, _ = worker.post(
            base + "lifecycle/attach/", {"nav": "navigate"})
        self.assertEqual(status, 200, body)
        worker.handle = json.loads(body)["handle_id"]
        for page in ("papers/", "survey/", "plots/", "export/markdown/"):
            status, body, _ = worker.get(base + page)
            self.assertEqual(status, 200)
            self.assertIn(b"Synthetic example", body)
            if page == "papers/":
                self.assertIn(b"Save project copy", body)
                self.assertNotIn(b"Legacy shared-DB mode", body)
        self.assertEqual(worker.get(base + "search/1/edit/")[0], 200)
        status, body, _ = worker.post(
            base + "search/1/edit/",
            {
                "name": "Synthetic submitted edit",
                "keywords_json": '["alpha", "beta"]',
                "venues_json": "[]",
                "author_names_json": "[]",
            },
        )
        self.assertEqual(status, 200, body[:1000])
        edited = CopyRecoveryTests.rows(sample)
        self.assertNotEqual(edited["literature_search"],
                            original["literature_search"])
        self.assertIn("Synthetic submitted edit",
                      str(edited["literature_search"]))
        for name in original.keys() - {"literature_search"}:
            self.assertEqual(edited[name], original[name])
        copied = self.work / "share copy"
        fields = {
            "action": "copy", "source": str(sample),
            "destination": str(copied),
        }
        status, preview, _ = self.manager.post(
            self.base + "/workspace/operation/", fields | {"preview": "1"})
        self.assertEqual(status, 200, preview[:1000])
        self.assertFalse(copied.exists())
        token = re.search(rb'name="preview_token" value="([^"]+)"', preview)
        self.assertIsNotNone(token, preview[:1000])
        status, body, _ = self.manager.post(
            self.base + "/workspace/operation/",
            fields | {"confirm": "on", "preview_token": token[1].decode()},
        )
        self.assertEqual(status, 200, body[:1000])
        self.assertEqual(CopyRecoveryTests.rows(copied), edited)
        self.assertNotEqual(
            json.loads((copied / "project.json").read_text())["project_uuid"],
            identity,
        )
        self.assertEqual({p.name for p in copied.iterdir()},
                         {"project.json", "project.sqlite3"})
        self.assertEqual(
            self.manager.post(self.base + "/workspace/open/",
                              {"directory": str(copied)})[0], 200
        )
        self.assertEqual(CopyRecoveryTests.rows(sample), edited)
        self.assertFalse((self.clone / "projects").exists())
        self.assertNotIn("Installing existing requirements",
                         self.log.read_text())

    def test_real_terminal_ctrl_c_finishes_admitted_edit(self):
        self.start(terminal=True)
        directory, _pid, port, epoch, handle, manifest = self.create()
        base = f"http://127.0.0.1:{port}/p/{manifest['project_uuid']}/{epoch}/project/1"
        writer = _Client(handle)
        self.assertEqual(writer.get(base + "/search/new/")[0], 200)
        self.assertEqual(
            writer.post(
                base + "/search/new/",
                {
                    "name": "Local configuration before online search",
                    "keywords_json": '["synthetic"]',
                    "venues_json": "[]",
                    "author_names_json": "[]",
                },
            )[0],
            200,
        )
        gate_status, gate_body, _ = writer.post(base + "/search/1/run/", {})
        self.assertEqual(gate_status, 200)
        self.assertIn(b"<h1>Settings</h1>", gate_body)
        self.assertIn(b'name="openalex_email"', gate_body)
        self.assertIn((self.base + "/settings/").encode(),
                      writer.get(base + "/search/new/")[1])
        control = _Client()
        self.assertEqual(control.get(base + "/lifecycle/drain/")[0], 200)
        db = directory / "project.sqlite3"
        with (
            sqlite3.connect(db, isolation_level=None) as lock,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            lock.execute("BEGIN EXCLUSIVE")
            pending = pool.submit(
                writer.post,
                base + "/search/new/",
                {
                    "name": "Accepted before Ctrl+C",
                    "keywords_json": '["synthetic"]',
                    "venues_json": "[]",
                    "author_names_json": "[]",
                },
            )
            self.assertTrue(
                wait_for(
                    lambda: (
                        json.loads(control.get(
                            base + "/lifecycle/drain/")[1])["in_flight"] == 1
                    ),
                    4,
                )
            )
            # Real terminal line discipline sends SIGINT.
            os.write(self.master, b"\x03")
            time.sleep(0.7)
            self.assertIsNone(self.proc.poll(),
                              "must wait for the admitted transaction")
            lock.rollback()
            self.assertIn(pending.result(timeout=20)[0], (200, "REFUSED"))
        self.assertEqual(self.proc.wait(timeout=40), 0, self.log.read_text())
        with sqlite3.connect(db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM literature_search WHERE name='Accepted before Ctrl+C'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute(
                "PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute(
                "PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(_Client().get(base + "/papers/")[0], "REFUSED")

    def test_crash_restart_closes_original_worker_before_reopen(self):
        self.start()
        directory, pid, port, epoch, _handle, manifest = self.create()
        old_url = f"http://127.0.0.1:{port}/p/{manifest['project_uuid']}/{epoch}/project/1/papers/"
        # Only this synthetic supervisor; workers see pipe EOF.
        self.proc.kill()
        self.proc.wait(timeout=10)
        self.assertTrue(
            wait_for(lambda: _Client().get(old_url)[0] == "REFUSED", 15))
        self.start()
        self.assertEqual(
            self.manager.post(
                self.base + "/workspace/operation/",
                {
                    "action": "close",
                    "source": str(directory),
                    "confirm": "on",
                },
            )[0],
            200,
        )
        registry = self.work / "state" / "local-survey-filter" / "registry.sqlite3"
        with sqlite3.connect(registry) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM workers").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM leases").fetchone()[0], 0)
        self.assertEqual(
            self.manager.post(self.base + "/workspace/open/",
                              {"directory": str(directory)})[0], 200
        )
        with sqlite3.connect(registry) as connection:
            rows = connection.execute(
                "SELECT pid, worker_epoch FROM workers").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], pid)
        self.assertNotEqual(rows[0][1], epoch)

    def test_supervisor_crash_still_commits_admitted_transaction(self):
        self.start()
        directory, _pid, port, epoch, handle, manifest = self.create()
        base = f"http://127.0.0.1:{port}/p/{manifest['project_uuid']}/{epoch}/project/1"
        writer = _Client(handle)
        self.assertEqual(writer.get(base + "/search/new/")[0], 200)
        control = _Client()
        self.assertEqual(control.get(base + "/lifecycle/drain/")[0], 200)
        db = directory / "project.sqlite3"
        with (
            sqlite3.connect(db, isolation_level=None) as lock,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            lock.execute("BEGIN EXCLUSIVE")
            pending = pool.submit(
                writer.post,
                base + "/search/new/",
                {
                    "name": "Accepted before supervisor crash",
                    "keywords_json": '["synthetic"]',
                    "venues_json": "[]",
                    "author_names_json": "[]",
                },
            )
            self.assertTrue(
                wait_for(
                    lambda: (
                        json.loads(control.get(
                            base + "/lifecycle/drain/")[1])["in_flight"] == 1
                    ),
                    4,
                )
            )
            # Only our Popen; worker ownership comes from pipe EOF.
            self.proc.kill()
            self.proc.wait(timeout=10)
            time.sleep(0.5)
            lock.rollback()
            self.assertIn(pending.result(timeout=20)[0], (200, "REFUSED"))
        self.assertTrue(wait_for(lambda: _Client().get(
            base + "/papers/")[0] == "REFUSED"))
        with sqlite3.connect(db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM literature_search WHERE name='Accepted before supervisor crash'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute(
                "PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute(
                "PRAGMA foreign_key_check").fetchall(), [])

    def test_busy_port_and_second_manager_leave_listener_untouched(self):
        """Refuse occupied ports and duplicate managers without signalling them.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            result = subprocess.run(
                [sys.executable, str(self.clone / "launch.py"),
                 "--port", str(port)],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Address already in use", result.stderr)
            self.assertIn("No unrelated listener was stopped", result.stderr)
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        self.start()
        result = subprocess.run(
            [sys.executable, str(self.clone / "launch.py"), "--port", "0"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already owns this XDG state", result.stderr)
        self.assertEqual(self.manager.get(self.base + "/")[0], 200)

    def test_implicit_admin_writes_refuse_and_checks_use_memory(self):
        """Reject implicit writes and retain safe in-memory developer checks.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        env = dict(self.env)
        env.pop("LOCAL_SURVEY_DB", None)
        for command in ("migrate", "flush", "shell", "export_markdown"):
            result = subprocess.run(
                [sys.executable, str(self.clone / "launch.py"),
                 "django", command],
                env=env,
                cwd=self.clone,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(
                "refusing the implicit projects/db.sqlite3", result.stderr)
        for arguments in (("check",), ("makemigrations", "--check", "--dry-run")):
            result = subprocess.run(
                [sys.executable, str(self.clone / "launch.py"),
                 "django", *arguments],
                env=env,
                cwd=self.clone,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.clone / "projects").exists())

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from config.settings import DATABASES; print(DATABASES['default']['NAME'])",
            ],
            env=env,
            cwd=self.clone,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), ":memory:")

        # A present legacy file must not even be opened by an implicit command.
        projects = self.clone / "projects"
        projects.mkdir()
        old = projects / "db.sqlite3"
        old.write_bytes(b"synthetic legacy sentinel, intentionally not SQLite")
        before = old.read_bytes(), old.stat().st_mtime_ns
        try:
            result = subprocess.run(
                [sys.executable, str(self.clone / "launch.py"),
                 "django", "migrate"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(
                (old.read_bytes(), old.stat().st_mtime_ns), before)
            # Deliberate opt-in still targets this default path, not memory.
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from config.settings import DATABASES; print(DATABASES['default']['NAME'])",
                ],
                env=dict(env, LOCAL_SURVEY_LEGACY_SHARED_DB="1"),
                cwd=self.clone,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(old))
        finally:
            old.unlink()
            projects.rmdir()

    def test_help_and_unsupported_startup_do_not_create_state(self):
        """Retain discoverable help and reject invalid manager/DB startup.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        env = dict(self.env)
        env.pop("LOCAL_SURVEY_DB", None)
        for arguments, code in (
            (("--help",), 0),
            (("django", "help", "runserver"), 0),
            (("--noreload",), 2),
            (("--nostatic",), 2),
            (("--port", "0.0.0.0:8000"), 2),
            (("--port", "65536"), 2),
            (("django", "runserver", "--noreload"), 2),
        ):
            result = subprocess.run(
                [sys.executable, str(self.clone / "launch.py"), *arguments],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, code, result.stderr)
            if arguments == ("--help",):
                self.assertIn("New / Load manager", result.stdout)
                self.assertIn("--legacy-shared-db", result.stdout)
            elif code == 0:
                self.assertIn("runserver", result.stdout)
                self.assertIn("--noreload", result.stdout)
            self.assertFalse((self.clone / "projects").exists())
            self.assertFalse((self.work / "state").exists())

    def test_explicit_developer_server_and_admin_target_preserved(self):
        """Use explicit developer targets without manager bootstrap or state.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        env = dict(self.env, LOCAL_SURVEY_DB=str(
            self.work / "developer.sqlite3"))
        result = subprocess.run(
            [sys.executable, str(self.clone / "launch.py"),
             "django", "migrate", "--noinput"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.log.open("a") as stream:
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    str(self.clone / "launch.py"),
                    "django",
                    "runserver",
                    f"127.0.0.1:{port}",
                    "--legacy-shared-db",
                    "--noreload",
                ],
                env=env,
                stdout=stream,
                stderr=stream,
            )
        self.assertTrue(
            wait_for(
                lambda: (
                    f"http://127.0.0.1:{port}/" in self.log.read_text()
                    or self.proc.poll() is not None
                )
            )
        )
        self.assertIsNone(self.proc.poll(), self.log.read_text())
        status, body, _ = _Client().get(f"http://127.0.0.1:{port}/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"Offline work is ready", body)
        self.assertNotIn("Manager PID", self.log.read_text())
        self.assertFalse((self.clone / "projects").exists())
        self.assertFalse((self.work / "state").exists())

    def test_first_install_offline_failure_is_readable(self):
        """Force a genuine empty offline install and assert its readable failure.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        wheelhouse = self.work / "empty wheelhouse"
        wheelhouse.mkdir()
        env = dict(
            self.env,
            XDG_CACHE_HOME=str(self.work / "empty cache"),
            PIP_CACHE_DIR=str(self.work / "empty pip cache"),
            PIP_NO_INDEX="1",
            PIP_FIND_LINKS=str(wheelhouse),
            PIP_INDEX_URL="",
            PIP_EXTRA_INDEX_URL="",
            PIP_CONFIG_FILE=os.devnull,
        )
        result = subprocess.run(
            [sys.executable, str(self.clone / "launch.py"), "--port", "0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dependency installation failed", result.stderr)
        self.assertIn("connectivity/cache", result.stderr)
        self.assertFalse((self.clone / "projects").exists())
        self.assertNotIn("Manager PID", result.stdout)
        self.assertFalse((self.work / "state").exists())

    def test_explicit_synthetic_legacy_env_opt_in(self):
        legacy = self.work / "old credentials.env"
        legacy.write_text("OPENALEX_EMAIL=old-synthetic@example.org\n")
        self.env.pop("PYTHON_DOTENV_DISABLED")
        self.start(["--legacy-env", str(legacy)])
        self.assertIn(b"old-synthetic@example.org",
                      self.manager.get(self.base + "/settings/")[1])
        self.assertEqual(
            self.manager.post(
                self.base +
                "/settings/", {"openalex_email": "updated-synthetic@example.org"}
            )[0],
            200,
        )
        self.assertIn("updated-synthetic", legacy.read_text())
        self.assertFalse(
            (self.work / "state" / "local-survey-filter" / "credentials.env").exists())
