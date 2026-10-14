"""
Check launcher ownership, DB guards and bootstrap hashes in synthetic state.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_launcher_runtime.py

@Brief:  Check launcher ownership, DB guards and bootstrap hashes in synthetic state.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from socket import socketpair
from unittest.mock import Mock, patch

# Custom library Imports
from django.test import SimpleTestCase

from config import launcher

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Runtime guards are exercised without the launcher facade ---


class LauncherRuntimeTests(SimpleTestCase):
    """
    Keep startup selection independent of argparse and Django state.
    """

    def test_idle_request_line_timeout_is_quiet_and_other_errors_propagate(
        self,
    ) -> None:
        """
        Close idle sockets without hiding parsed-request or unrelated errors.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Exercise local socket pairs only; no server, project database or
            process-level signal handlers are started.
        """
        from django.core.servers.basehttp import (
            ThreadedWSGIServer,
            WSGIRequestHandler,
        )

        with (
            patch("literature.worker.bound_store", return_value={"ok": True}),
            patch.object(ThreadedWSGIServer, "__init__", autospec=True,
                         side_effect=RuntimeError("capture handler")) as create,
            self.assertRaisesRegex(RuntimeError, "capture handler"),
        ):
            launcher.serve(0, worker=True)
        server, address, handler_class = create.call_args.args
        self.assertEqual(address, ("127.0.0.1", 0))
        self.assertFalse(server.daemon_threads)
        self.assertTrue(server.block_on_close)
        accepted = Mock()
        with patch.object(ThreadedWSGIServer, "get_request",
                          return_value=(accepted, address)):
            self.assertEqual(server.get_request(), (accepted, address))
        accepted.settimeout.assert_called_once_with(30)

        for prefix in (b"", b"GET /"):
            peer, request = socketpair()
            with self.subTest(prefix=prefix), peer, request:
                request.settimeout(0.01)
                if prefix:
                    peer.sendall(prefix)
                handler = handler_class(request, ("127.0.0.1", 0), server)
                self.assertTrue(handler.close_connection)

        handler = handler_class.__new__(handler_class)
        handler.raw_requestline = b"GET /previous HTTP/1.1\r\n"
        handler.close_connection = False
        handler.rfile = Mock()
        handler.rfile.readline.side_effect = TimeoutError("keep-alive idle")
        handler.handle_one_request()
        self.assertEqual(handler.raw_requestline, b"")
        self.assertTrue(handler.close_connection)

        handler.rfile.readline.side_effect = [
            b"GET / HTTP/1.1\r\n", TimeoutError("headers timed out")]
        with self.assertRaisesRegex(TimeoutError, "headers timed out"):
            handler.handle_one_request()
        self.assertEqual(handler.raw_requestline, b"GET / HTTP/1.1\r\n")
        handler.rfile.readline.side_effect = RuntimeError("unexpected error")
        with self.assertRaisesRegex(RuntimeError, "unexpected error"):
            handler.handle_one_request()

        with patch.object(WSGIRequestHandler, "handle_one_request",
                          autospec=True) as process:
            handler.handle_one_request()
        process.assert_called_once_with(handler)

    def test_admin_and_legacy_role_guards_precede_django(self) -> None:
        """
        Reject implicit writes and nondeveloper legacy opt-ins before setup.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("django.core.management.execute_from_command_line")
            as execute,
            patch.object(launcher, "bootstrap") as bootstrap,
            patch.object(launcher, "configure") as configure,
        ):
            for command in (
                "migrate", "flush", "shell", "export_markdown", "unknown",
                "runserver",
            ):
                with self.subTest(command=command):
                    self.assertEqual(launcher.run_django([command]), 2)
            for role in ("manager", "worker", "invalid"):
                with self.subTest(role=role):
                    os.environ["WP_ROLE"] = role
                    self.assertEqual(launcher.run_django(
                        ["migrate", "--legacy-shared-db"]), 2)
            execute.assert_not_called()
            bootstrap.assert_not_called()
            configure.assert_not_called()
            self.assertNotIn("DJANGO_SETTINGS_MODULE", os.environ)
            self.assertNotIn("LOCAL_SURVEY_LEGACY_SHARED_DB", os.environ)

    def test_safe_dispatch_defaults_to_memory_without_private_state(
        self,
    ) -> None:
        """
        Inspect fresh settings for every untargeted safe-command contract.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        script = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
from config import launcher
with patch('django.core.management.execute_from_command_line') as execute:
    assert launcher.run_django(sys.argv[1:]) == 0
    execute.assert_called_once_with([str(launcher.ROOT / 'launch.py'),
                                   *sys.argv[1:]])
from config.settings import DATABASES
assert DATABASES['default']['NAME'] == ':memory:'
assert 'LOCAL_SURVEY_DB' not in os.environ
assert 'LOCAL_SURVEY_CREDENTIALS_FILE' not in os.environ
assert not Path(os.environ['XDG_DATA_HOME']).exists()
assert not Path(os.environ['XDG_CACHE_HOME']).exists()
"""
        with tempfile.TemporaryDirectory(prefix="launcher-memory-") as tmp:
            env = {
                "PATH": os.defpath, "HOME": tmp,
                "XDG_DATA_HOME": str(Path(tmp) / "state"),
                "XDG_CACHE_HOME": str(Path(tmp) / "cache"),
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            for arguments in (
                ["check"], ["test"], ["makemigrations"], ["help"],
                ["version"], ["--version"], ["migrate", "--help"], [],
            ):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [sys.executable, "-c", script, *arguments],
                        cwd=launcher.ROOT, env=env, capture_output=True,
                        text=True, timeout=15, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_target_and_legacy_opt_in_preserve_arguments(
        self,
    ) -> None:
        """
        Keep DB ownership and opaque Django flags without manager setup.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for env, arguments in (
            ({"LOCAL_SURVEY_DB": ":memory:"},
             ["migrate", "--noinput", "--database", "default"]),
            ({}, ["runserver", "127.0.0.1:0", "--legacy-shared-db",
                  "--noreload"]),
        ):
            with (
                self.subTest(arguments=arguments),
                patch.dict(os.environ, env, clear=True),
                patch("django.core.management.execute_from_command_line")
                as execute,
                patch.object(launcher, "bootstrap") as bootstrap,
                patch.object(launcher, "configure") as configure,
            ):
                self.assertEqual(launcher.run_django(arguments), 0)
                execute.assert_called_once_with([
                    str(launcher.ROOT / "launch.py"),
                    *[arg for arg in arguments if arg != "--legacy-shared-db"],
                ])
                if env:
                    self.assertEqual(os.environ["LOCAL_SURVEY_DB"], ":memory:")
                    self.assertNotIn(
                        "LOCAL_SURVEY_LEGACY_SHARED_DB", os.environ)
                else:
                    self.assertEqual(
                        os.environ["LOCAL_SURVEY_LEGACY_SHARED_DB"], "1")
                bootstrap.assert_not_called()
                configure.assert_not_called()

    def test_bootstrap_hash_repeat_repair_and_exec_arguments(self) -> None:
        """
        Keep successful repeats pip-free and require deliberate hash repair.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="launcher-hash-") as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            requirements = source / "requirements.txt"
            requirements.write_text("django>=5.0,<6.0\n")
            cache = root / "cache"
            env = cache / "local-survey-filter" / "envs" / hashlib.sha256(
                str(source).encode()).hexdigest()[:16]
            (env / "bin").mkdir(parents=True)
            (env / "bin" / "python").touch()
            marker = env / "requirements.sha256"
            digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
            marker.write_text(digest)
            with (
                patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}),
                patch.object(launcher, "ROOT", source),
                patch.object(sys, "prefix", str(env)),
                patch.object(launcher.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                launcher.bootstrap()
                self.assertEqual(run.call_count, 1)
                self.assertIn("import django", run.call_args.args[0][2])
                self.assertEqual(marker.read_text(), digest)
                requirements.write_text("django>=5.1,<6.0\n")
                run.reset_mock()
                with self.assertRaisesRegex(RuntimeError,
                                            "requirements.txt changed"):
                    launcher.bootstrap()
                run.assert_not_called()
                self.assertEqual(marker.read_text(), digest)
                with (
                    patch.object(sys, "prefix", str(root / "developer")),
                    patch.object(launcher.os, "execv") as execute,
                ):
                    launcher.bootstrap(True, arguments=[
                        "--install", "--port", "0", "--legacy-env",
                        "/synthetic/credentials with spaces.env",
                    ])
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args_list[0].args[0][1:4],
                                 ["-m", "pip", "install"])
                self.assertEqual(marker.read_text(), hashlib.sha256(
                    requirements.read_bytes()).hexdigest())
                python = str(env / "bin" / "python")
                execute.assert_called_once_with(python, [
                    python, str(source / "launch.py"), "--port", "0",
                    "--legacy-env", "/synthetic/credentials with spaces.env",
                ])

    def test_worker_binding_skips_bootstrap_and_invalid_binding_refuses(
        self,
    ) -> None:
        """
        Require owner/lock descriptors before serving an internal worker.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(launcher, "bootstrap") as bootstrap,
            patch.object(launcher, "configure") as configure,
            patch.object(launcher, "serve") as serve,
        ):
            self.assertEqual(launcher.run_manager(0, worker=True), 1)
            serve.assert_not_called()
            os.environ.update(WP_ROLE="worker", WP_OWNER_FD="3",
                              WP_WORKER_LOCK_FD="4")
            self.assertEqual(launcher.run_manager(0, worker=True), 0)
            serve.assert_called_once_with(0, worker=True)
            bootstrap.assert_not_called()
            configure.assert_not_called()

    def test_thin_cli_import_and_plain_value_delegation(self) -> None:
        """
        Keep CLI import inert and pass plain values to both runtime paths.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        script = """
import sys
from unittest.mock import patch
import launch
from config import launcher
assert 'django' not in sys.modules
with patch.object(launcher, 'run_manager', return_value=0) as manager:
    assert launch.main(['--port', '0', '--install']) == 0
    manager.assert_called_once_with(
        0, install=True, legacy_env=None, worker=False,
        arguments=['--port', '0', '--install'])
with patch.object(launcher, 'run_django', return_value=0) as developer:
    assert launch.main(['django', 'migrate', '--noinput']) == 0
    developer.assert_called_once_with(['migrate', '--noinput'])
"""
        with tempfile.TemporaryDirectory(prefix="launcher-cli-") as tmp:
            env = {
                "PATH": os.defpath, "HOME": tmp,
                "PYTHON_DOTENV_DISABLED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=launcher.ROOT, env=env,
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [sys.executable, "launch.py", "--help"], cwd=launcher.ROOT,
                env=env, capture_output=True, text=True, timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--port", result.stdout)
            self.assertIn("django", result.stdout)
            self.assertNotIn("--worker", result.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])
