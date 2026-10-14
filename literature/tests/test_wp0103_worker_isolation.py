#!/usr/bin/env python3

"""
Test UUID-and-epoch worker isolation over real HTTP.

WP01.03: real worker HTTP proves UUID+epoch isolation (synthetic stores).

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_wp0103_worker_isolation.py

@Brief:  Test UUID-and-epoch worker isolation over real HTTP.

@Date:   03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# @File: test_wp0103_worker_isolation.py
# @Date: 03/10/2026

# Regular library imports
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent import futures
from http import cookiejar
from pathlib import Path
from uuid import UUID

# Custom library Imports
from django.conf import settings
from django.test import SimpleTestCase

from literature.project_store import (
    create_project_store,
    init_app_registry,
    open_project_store,
)

# ------------------------------------------------------------------------ #
# Embedded child scripts (each worker binds its default DB once at startup)
# ------------------------------------------------------------------------ #

# ponytail: threaded WSGI in a child; thedev server stays out of the proof.
_SERVER_SCRIPT = """
import os
import sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.handlers.wsgi import WSGIHandler
from django.core.servers.basehttp import WSGIRequestHandler


class Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


server = Threaded(("127.0.0.1", 0), WSGIRequestHandler)
server.set_app(WSGIHandler())
print(f"READY {server.server_port}", flush=True)
server.serve_forever()
"""

_SEED_SCRIPT = """
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from literature.models import Discovery, Paper, Project, Search
from literature.services.search_service import gate_papers

project = Project.objects.get()
project.ensure_default_stages()
search = Search.objects.create(
    project=project, name="S", keywords=["shared"], query="shared"
)
paper = Paper.objects.create(
    title="Shared title", abstract=sys.argv[1],
    doi="10.1000/shared", sources=["openalex"],
)
Discovery.objects.create(
    paper=paper, search=search, source="openalex", query="shared"
)
gate_papers([paper], project.stages.get(name="Title"))
print(f"OK project={project.pk} paper={paper.pk}", flush=True)
"""

_FAIL_SCRIPT = """
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import transaction

from literature.models import Paper

try:
    with transaction.atomic():
        Paper.objects.create(title="Will roll back A")
        Paper.objects.create(title="Will roll back B")
        raise RuntimeError("WP01.03 deliberate failure")
except RuntimeError:
    pass
print(f"OK papers={Paper.objects.count()}", flush=True)
"""

_SETTINGS_SCRIPT = """
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.conf import settings as s

print("ENGINE=" + s.SESSION_ENGINE, flush=True)
print("SESSION=" + s.SESSION_COOKIE_NAME, flush=True)
print("CSRF=" + s.CSRF_COOKIE_NAME, flush=True)
print("SPATH=" + s.SESSION_COOKIE_PATH, flush=True)
print("CPATH=" + s.CSRF_COOKIE_PATH, flush=True)
"""


# ------------------------------------------------------------------------ #
# Proof tests
# ------------------------------------------------------------------------ #


class WorkerIsolationTests(SimpleTestCase):
    """
    Two threaded workers + manager over real HTTP; synthetic stores only.
    """

    # --- Clean env so test children never inherit a production worker env ---
    @staticmethod
    def _child_env(extra: dict[str, str]) -> dict[str, str]:
        """
        Replace inherited worker identity and DB bindings for a child.

        Args:
            extra (dict[str, str]): Environment overrides to apply after
                removing the inherited role, UUID, epoch and DB target.

        Kwargs:
            None

        Returns:
            dict[str, str]: Copied environment with the child overrides.
        """
        env = dict(os.environ)
        for key in ("WP_ROLE", "WP_PROJECT_UUID", "WP_WORKER_EPOCH", "LOCAL_SURVEY_DB"):
            env.pop(key, None)
        env.update(extra)
        return env

    # --- One ORM/file operation inside a fixed-default worker ---
    @staticmethod
    def _run_script(db: Path, script: str, *args: str, extra_env=None) -> str:
        """
        Run a script in a child initially bound to a synthetic store.

        Args:
            db (Path): Default SQLite target before environment overrides.
            script (str): Python source to execute in the child.
            *args (str): Additional arguments passed to the script.

        Kwargs:
            extra_env (dict | None): Additional environment overrides,
                including identity or DB binding. Defaults to None.

        Returns:
            str: Stripped stdout from the successful child process.

        Raises:
            AssertionError: If the child exits unsuccessfully.
            subprocess.TimeoutExpired: If the child exceeds 300 seconds.
        """
        env = WorkerIsolationTests._child_env({"LOCAL_SURVEY_DB": str(db)})
        env.update(extra_env or {})
        completed = subprocess.run(
            [sys.executable, "-c", script, *args],
            capture_output=True, text=True, timeout=300, check=False,
            cwd=Path(settings.BASE_DIR), env=env,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        return completed.stdout.strip()

    # --- Real threaded HTTP worker bound to one store file + identity ---
    @staticmethod
    def _launch(role, db=None, store_uuid="", epoch="", ready_path="/settings/"):
        """
        Start a threaded HTTP child and register it for class teardown.

        Args:
            role (str): Manager or worker role assigned to the child.

        Kwargs:
            db (Path | None): Synthetic default DB target. Defaults to None,
                leaving the target unset in the cleaned child environment.
            store_uuid (str): Worker project UUID. Defaults to an empty
                string, which leaves the UUID unset.
            epoch (str): Worker epoch. Defaults to an empty string, which
                leaves the epoch unset.
            ready_path (str | None): Path to poll for HTTP readiness.
                Defaults to "/settings/"; None skips HTTP polling.

        Returns:
            str: Loopback base URL using the child's assigned port.

        Raises:
            AssertionError: If no port is reported, the child exits, or
                readiness polling fails.
        """
        extra = {"WP_ROLE": role}
        if db is not None:
            extra["LOCAL_SURVEY_DB"] = str(db)
        if store_uuid:
            extra["WP_PROJECT_UUID"] = store_uuid
        if epoch:
            extra["WP_WORKER_EPOCH"] = epoch
        proc = subprocess.Popen(
            [sys.executable, "-c", _SERVER_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=Path(settings.BASE_DIR),
            env=WorkerIsolationTests._child_env(extra),
        )
        assert proc.stdout is not None
        port = 0
        deadline = time.time() + 60
        while time.time() < deadline:
            line = proc.stdout.readline()
            if "READY" in line:
                port = int(line.split()[-1])
                break
            if proc.poll() is not None:
                raise AssertionError(
                    f"worker exited: {proc.stdout.read()[-2000:]}")
        assert port != 0, "worker did not report a port"
        base = f"http://127.0.0.1:{port}"
        if ready_path is not None:
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(
                        base + ready_path, timeout=5).close()
                    break
                except urllib.error.URLError:
                    time.sleep(0.2)
            else:
                proc.terminate()
                raise AssertionError("worker never answered HTTP")
        WorkerIsolationTests._servers.append(proc)
        return base

    # --- Per-worker browser jar: Path-scoped cookies never cross workers ---
    @staticmethod
    def _browser(base):
        """
        Create a cookie-aware opener and its initially empty jar.

        Args:
            base (str): Worker base URL accepted but not used to scope the jar.

        Kwargs:
            None

        Returns:
            tuple: URL opener and CookieJar; response cookie paths scope
                subsequent requests.
        """
        jar = cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        return opener, jar

    # --- GET through a worker jar; returns (status, body, set_cookie_headers) ---
    def _get(self, opener, url):
        """
        Read a GET response or HTTP error through a cookie-aware opener.

        Args:
            opener (urllib.request.OpenerDirector): Opener used for the GET.
            url (str): Full request URL.

        Kwargs:
            None

        Returns:
            tuple: Status code, body bytes and Set-Cookie header values.
        """
        try:
            with opener.open(url, timeout=20) as response:
                return response.status, response.read(), response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get_all("Set-Cookie") or []

    # --- POST JSON with the jar CSRF cookie (real browser flow) ---
    def _post_json(self, opener, jar, url, payload, handle=""):
        """
        POST JSON with the jar's worker CSRF token and optional tab handle.

        Args:
            opener (urllib.request.OpenerDirector): Cookie-aware request opener.
            jar (cookiejar.CookieJar): Jar supplying a worker CSRF token.
            url (str): Full request URL.
            payload (object): JSON-serializable request body.

        Kwargs:
            handle (str): Tab handle sent as X-Project-Handle. Defaults to an
                empty string, which omits the header.

        Returns:
            tuple: Status code and body bytes, including HTTP error responses.
        """
        token = next(
            (c.value for c in jar if c.name.startswith("wpcsrft_")), "")
        data = json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("X-CSRFToken", token)
        if handle:
            # ponytail: worker mutations carry the tab handle; epoch-gated.
            request.add_header("X-Project-Handle", handle)
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    # --- Raw single-shot HTTP (never follows redirects; real status/Location) ---
    @staticmethod
    def _raw_post(base, path, body, cookie="", csrf="", handle=""):
        """
        POST form data once without following redirects.

        Args:
            base (str): HTTP origin providing the hostname and port.
            path (str): Request path on that origin.
            body (str): URL-encoded form body.

        Kwargs:
            cookie (str): Cookie header value. Defaults to an empty string,
                which omits the header.
            csrf (str): X-CSRFToken header value. Defaults to an empty string,
                which omits the header.
            handle (str): Tab handle appended as a wp_handle form field.
                Defaults to an empty string, which leaves the body unchanged.

        Returns:
            tuple: Status code, Location header or empty string, and body bytes.
        """
        import http.client
        from urllib.parse import urlsplit

        parts = urlsplit(base)
        if handle:
            body = body + ("&" if body else "") + f"wp_handle={handle}"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRFToken"] = csrf
        connection = http.client.HTTPConnection(
            parts.hostname, parts.port, timeout=20)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Location") or "", response.read()
        finally:
            connection.close()

    # --- Cookie/CSRF headers from one worker jar (Path-scoped by the server) ---
    @staticmethod
    def _jar_headers(jar):
        """
        Build Cookie and worker-only CSRF header values from a jar.

        Args:
            jar (cookiejar.CookieJar): Cookies collected from worker responses.

        Kwargs:
            None

        Returns:
            tuple[str, str]: Combined cookies and the worker CSRF token, or
                an empty token when no matching cookie exists.
        """
        cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
        # ponytail: exact worker cookie only; no global csrftoken fallback.
        csrf = next((c.value for c in jar if c.name.startswith("wpcsrft_")), "")
        return cookie, csrf

    # --- Class fixtures: two stores, overlapping PK/DOI, three HTTP servers ---
    @classmethod
    def setUpClass(cls):
        """
        Seed two synthetic stores and launch their workers and a manager.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Override XDG paths, launch HTTP children and register tab handles.
        """
        super().setUpClass()
        cls._servers: list[subprocess.Popen] = []
        cls._tmp = tempfile.TemporaryDirectory(prefix="wp0103-http-")
        tmp = Path(cls._tmp.name)
        # ponytail (WP01.05 reopen): isolated registry so tab-handle rows stay
        # synthetic; worker POSTs below carry genuine registered handles.
        cls._old_xdg = (os.getenv("XDG_DATA_HOME"),
                        os.getenv("XDG_CACHE_HOME"))
        os.environ["XDG_DATA_HOME"] = str(tmp / "xdg-data")
        os.environ["XDG_CACHE_HOME"] = str(tmp / "xdg-cache")
        live = Path(settings.BASE_DIR) / "projects" / "db.sqlite3"
        cls._live_mtime = live.stat().st_mtime if live.exists() else None
        helper = WorkerIsolationTests
        cls.manifest_a = create_project_store(tmp / "projA", "Project A")
        cls.manifest_b = create_project_store(tmp / "projB", "Project B")
        cls.db_a = cls.manifest_a.location / "project.sqlite3"
        cls.db_b = cls.manifest_b.location / "project.sqlite3"
        helper._run_script(cls.db_a, _SEED_SCRIPT, "abstract-A")
        helper._run_script(cls.db_b, _SEED_SCRIPT, "abstract-B")
        cls.uuid_a, cls.uuid_b = str(cls.manifest_a.project_uuid), str(
            cls.manifest_b.project_uuid)
        cls.epoch_a, cls.epoch_b = "epoch-a1", "epoch-b1"
        cls.http_a = helper._launch("worker", cls.db_a, cls.uuid_a, cls.epoch_a,
                                    f"/p/{cls.uuid_a}/{cls.epoch_a}/project/1/papers/")
        cls.http_b = helper._launch("worker", cls.db_b, cls.uuid_b, cls.epoch_b,
                                    f"/p/{cls.uuid_b}/{cls.epoch_b}/project/1/papers/")
        cls.http_mgr = helper._launch(
            "manager", tmp / "private-manager.sqlite3")
        cls.url_a = f"{cls.http_a}/p/{cls.uuid_a}/{cls.epoch_a}"
        cls.url_b = f"{cls.http_b}/p/{cls.uuid_b}/{cls.epoch_b}"
        cls.opener_a, cls.jar_a = helper._browser(cls.http_a)
        cls.opener_b, cls.jar_b = helper._browser(cls.http_b)
        cls.opener_m, cls.jar_m = helper._browser(cls.http_mgr)
        # ponytail: genuine tab handles for the worker mutation posts below;
        # epochs are fixed here, so registration cannot join a wrong worker.
        from urllib.parse import urlsplit

        from literature import workspace as ws

        cls.handle_a = ws.register_handle(
            str(cls.manifest_a.location), cls.uuid_a, cls.epoch_a,
            urlsplit(cls.http_a).port or 0)
        cls.handle_b = ws.register_handle(
            str(cls.manifest_b.location), cls.uuid_b, cls.epoch_b,
            urlsplit(cls.http_b).port or 0)

    @classmethod
    def tearDownClass(cls):
        """
        Stop HTTP children, restore XDG paths and check the live DB mtime.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for proc in cls._servers:
            proc.terminate()
        for proc in cls._servers:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for key, value in zip(("XDG_DATA_HOME", "XDG_CACHE_HOME"), cls._old_xdg):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()
        live = Path(settings.BASE_DIR) / "projects" / "db.sqlite3"
        if cls._live_mtime is not None and live.exists():
            assert live.stat().st_mtime == cls._live_mtime, "live DB touched!"
        super().tearDownClass()

    # --- Store rows via stdlib (parent never opens Django ORM on them) ---
    @staticmethod
    def _paper_rows(db: Path):
        """
        Read synthetic paper content in primary-key order.

        Args:
            db (Path): Synthetic SQLite store to inspect.

        Kwargs:
            None

        Returns:
            list[tuple]: Paper ID, title, abstract and DOI rows ordered by ID.
        """
        with sqlite3.connect(db) as connection:
            return list(connection.execute("SELECT id, title, abstract, doi FROM literature_paper ORDER BY id"))

    @staticmethod
    def _decision_rows(db: Path):
        """
        Read synthetic screening decisions in paper and stage order.

        Args:
            db (Path): Synthetic SQLite store to inspect.

        Kwargs:
            None

        Returns:
            list[tuple]: Paper ID, stage name and decision rows.
        """
        with sqlite3.connect(db) as connection:
            return list(connection.execute(
                "SELECT d.paper_id, s.name, d.decision FROM literature_screeningdecision d"
                ' JOIN literature_stage s ON s.id = d.stage_id ORDER BY d.paper_id, s."order"'))

    # --- Overlapping PKs/DOIs diverge independently over concurrent HTTP ---
    def test_concurrent_reads_stay_isolated(self) -> None:
        """
        Keep abstracts distinct during concurrent reads with shared IDs/DOIs.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        targets = [f"{self.url_a}/project/1/papers/" for _ in range(4)]
        targets += [f"{self.url_b}/project/1/papers/" for _ in range(4)]
        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            # ponytail: 8 concurrent HTTP clients; threaded dev servers serialize
            # handling, which still proves request routing, not same-instant writes.
            results = list(pool.map(lambda args: self._get(*args),
                                    [(self.opener_a, u) if "/p/" + self.uuid_a in u else (self.opener_b, u) for u in targets]))
        for (status, body, _), url in zip(results, targets):
            self.assertEqual(status, 200, url)
            text = body.decode()
            if "/p/" + self.uuid_a in url:
                self.assertIn("Shared title", text)
            else:
                self.assertIn("Shared title", text)
        rows_a, rows_b = self._paper_rows(
            self.db_a), self._paper_rows(self.db_b)
        self.assertEqual([r[0] for r in rows_a], [1])
        self.assertEqual([r[0] for r in rows_b], [1])
        self.assertEqual(rows_a[0][3], rows_b[0][3], "same DOI in both stores")
        self.assertEqual(rows_a[0][2], "abstract-A")
        self.assertEqual(rows_b[0][2], "abstract-B")

    # --- Wrong/stale/missing identity fails closed without writing ---
    def test_wrong_identity_fails_closed(self) -> None:
        """
        Reject wrong, stale and missing prefixes without changing papers.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        before = (self._paper_rows(self.db_a), self._paper_rows(self.db_b))
        bad_uuid = f"{self.http_a}/p/{self.uuid_b}/{self.epoch_a}/project/1/papers/"
        stale = f"{self.http_a}/p/{self.uuid_a}/stale-epoch/project/1/papers/"
        naked = f"{self.http_a}/project/1/papers/"
        for url in (bad_uuid, stale, naked):
            status, _, _ = self._get(self.opener_a, url)
            self.assertEqual(status, 404, url)
        # ponytail: workers never serve settings/admin/credentials, prefixed or not.
        for url in (f"{self.http_a}/settings/", f"{self.http_a}/admin/",
                    f"{self.url_a}/settings/", f"{self.url_a}/admin/"):
            status, _, _ = self._get(self.opener_a, url)
            self.assertEqual(status, 404, url)
        self.assertEqual((self._paper_rows(self.db_a),
                         self._paper_rows(self.db_b)), before)

    # --- Parent/manager never serves or mutates literature records ---
    def test_manager_cannot_serve_research(self) -> None:
        """
        Render the manager landing but reject research reads and decisions.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        # ponytail: WP01.04 gives the manager a manifest-only landing at /;
        # research rows stay fail-closed behind it.
        status, body, _ = self._get(self.opener_m, self.http_mgr + "/")
        self.assertEqual(status, 200, "/")
        self.assertIn(b'workspace-mode', body)
        self.assertNotIn(b'Shared title', body)
        for path in ("/project/1/", "/project/1/papers/",
                     "/screening/decide/", "/settings/"):
            status, _, _ = self._get(self.opener_m, self.http_mgr + path)
            if path.startswith("/settings"):
                self.assertEqual(status, 200, path)
            else:
                self.assertEqual(status, 404, path)
        status, _ = self._post_json(self.opener_m, self.jar_m,
                                    self.http_mgr + "/screening/decide/",
                                    {"paper_id": 1, "stage_id": 1, "decision": "ACCEPT"})
        self.assertEqual(status, 404)

    # --- Independent decisions: HTTP ACCEPT on A leaves B pending ---
    def test_http_decision_on_a_leaves_b_pending(self) -> None:
        """
        Accept A's title over HTTP while B stays pending and URLs stay bound.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        status, _, _ = self._get(
            self.opener_a, f"{self.url_a}/project/1/screen/title/")
        self.assertEqual(status, 200)
        status, body = self._post_json(self.opener_a, self.jar_a,
                                       f"{self.url_a}/screening/decide/",
                                       {"paper_id": 1, "stage_id": 1,
                                           "decision": "ACCEPT"},
                                       handle=self.handle_a)
        self.assertEqual(status, 200, body[:500])
        # ponytail: reverse() keeps the worker prefix in JSON URLs; a
        # hardcoded /project path would strand the next browser request.
        next_url = json.loads(body.decode())["next_paper_url"]
        self.assertTrue(next_url.startswith(
            f"/p/{self.uuid_a}/{self.epoch_a}/"), next_url)
        decisions_a = {(p, s): d for p, s, d in self._decision_rows(self.db_a)}
        decisions_b = {(p, s): d for p, s, d in self._decision_rows(self.db_b)}
        self.assertEqual(decisions_a.get((1, "Title")), "ACCEPT")
        self.assertEqual(decisions_b.get((1, "Title")), "PENDING")

    # --- Misconfigured worker (right file, wrong UUID) serves nothing ---
    def test_misconfigured_worker_serves_nothing(self) -> None:
        """
        Serve no research when a worker's UUID disagrees with its store.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        bad_base = self._launch("worker", self.db_a, self.uuid_b, self.epoch_a,
                                ready_path=None)
        bad_browser, _ = self._browser(bad_base)
        for url in (f"{bad_base}/p/{self.uuid_b}/{self.epoch_a}/project/1/papers/",
                    f"{bad_base}/p/{self.uuid_a}/{self.epoch_a}/project/1/papers/",
                    f"{bad_base}/project/1/papers/",
                    f"{bad_base}/settings/"):
            status, _, _ = self._get(bad_browser, url)
            self.assertEqual(status, 404, url)
        self.assertEqual(len(self._paper_rows(self.db_a)), 1)

    # --- Genuine CSRF form POST: prefixed Location, followable, divergent ---
    def test_prefixed_form_post_diverges_metadata(self) -> None:
        """
        Enforce handle and CSRF gates before independent prefixed edits.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from urllib.parse import urlencode

        # ponytail: bare POST with no handle fails at the tab gate (410);
        # a valid handle without CSRF still fails at CSRF (403): the new
        # gate adds ownership without bypassing genuine CSRF either way.
        status, _, _ = self._raw_post(
            self.http_a, f"/p/{self.uuid_a}/{self.epoch_a}/project/1/papers/1/",
            urlencode({"action": "details", "title": "Title X"}))
        self.assertEqual(status, 410)
        status, _, _ = self._raw_post(
            self.http_a, f"/p/{self.uuid_a}/{self.epoch_a}/project/1/papers/1/",
            urlencode({"action": "details", "title": "Title X"}),
            handle=self.handle_a)
        self.assertEqual(status, 403)
        # ponytail: warm the jar CSRF cookie exactly like a browser tab would.
        status, _, _ = self._get(
            self.opener_a, f"{self.url_a}/project/1/screen/title/")
        self.assertEqual(status, 200)
        for base, url, jar, title in (
            (self.http_a, self.url_a, self.jar_a, "Title A"),
            (self.http_b, self.url_b, self.jar_b, "Title B"),
        ):
            store_uuid = self.uuid_a if base == self.http_a else self.uuid_b
            epoch = self.epoch_a if base == self.http_a else self.epoch_b
            cookie, csrf = self._jar_headers(jar)
            self.assertTrue(csrf, "browser jar holds no CSRF token")
            handle = self.handle_a if base == self.http_a else self.handle_b
            status, location, _ = self._raw_post(
                base, f"/p/{store_uuid}/{epoch}/project/1/papers/1/",
                urlencode({"action": "details", "title": title,
                           "doi": "10.1000/shared", "abstract": f"abstract-{'A' if title == 'Title A' else 'B'}"}),
                cookie=cookie, csrf=csrf, handle=handle)
            self.assertEqual(status, 302, (title, location))
            # ponytail: Location keeps the prefix, so the browser follow-up
            # stays inside the same worker instead of failing closed.
            self.assertTrue(location.startswith(
                f"/p/{store_uuid}/{epoch}/"), location)
            opener = self.opener_a if base == self.http_a else self.opener_b
            status, body, _ = self._get(opener, base + location)
            self.assertEqual(status, 200, location)
            self.assertIn(title, body.decode())
        titles = {self._paper_rows(
            self.db_a)[0][1], self._paper_rows(self.db_b)[0][1]}
        self.assertEqual(titles, {"Title A", "Title B"})

    # --- Concurrent workers render only their own prefixed links/fetch ---
    def test_rendered_links_keep_prefix(self) -> None:
        """
        Keep each worker's UUID and epoch in concurrently rendered pages.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """

        def fetch(args):
            """
            Fetch a worker page and decode its body for prefix assertions.

            Args:
                args (tuple): Cookie-aware opener and full page URL.

            Kwargs:
                None

            Returns:
                tuple[int, str]: HTTP status and decoded page body.
            """
            opener, url = args
            status, body, _ = self._get(opener, url)
            return status, body.decode()

        pages = [(self.opener_a, f"{self.url_a}/project/1/screen/title/"),
                 (self.opener_b, f"{self.url_b}/project/1/screen/title/"),
                 (self.opener_a, f"{self.url_a}/project/1/papers/"),
                 (self.opener_b, f"{self.url_b}/project/1/papers/")]
        # ponytail: concurrent threaded-server requests share threads; the
        # per-request prefix save/restore must not leak A into B.
        with futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch, pages * 2))
        for (status, text), (_, url) in zip(results, pages * 2):
            self.assertEqual(status, 200, url)
            own = self.uuid_a if "/p/" + self.uuid_a in url else self.uuid_b
            other = self.uuid_b if own == self.uuid_a else self.uuid_a
            epoch = self.epoch_a if own == self.uuid_a else self.epoch_b
            self.assertIn(f"/p/{own}/{epoch}/", text, url)
            self.assertNotIn(other, text, url)

    # --- Mutation/download/provider contexts all require the worker prefix ---
    def test_contexts_require_prefix(self) -> None:
        """
        Require prefixes for downloads and reject unbound provider POSTs.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        runs_before = self._table_count(self.db_a, "literature_searchrun")
        checks = [
            (f"{self.url_a}/project/1/export/markdown/", 200),
            (f"{self.url_a}/project/1/plots/download/", 200),
            (f"{self.url_a}/project/1/references/final/download/", 200),
        ]
        for url, want in checks:
            self.assertEqual(self._get(self.opener_a, url)[0], want, url)
        for naked in ("/project/1/export/markdown/", "/project/1/plots/download/",
                      "/project/1/references/final/download/"):
            self.assertEqual(
                self._get(self.opener_a, self.http_a + naked)[0], 404, naked)
        # ponytail: provider POSTs are bound by the same prefix gate; the 404
        # proves no SearchRun row could be written for a prefix-less request.
        status, _ = self._post_json(self.opener_a, self.jar_a,
                                    self.http_a + "/project/1/search/1/run/", {})
        self.assertEqual(status, 404)
        self.assertEqual(self._table_count(
            self.db_a, "literature_searchrun"), runs_before)

    # --- Busy A never writes into B; failed atomic rolls back only its store ---
    def test_busy_and_rollback_isolation(self) -> None:
        """
        Keep B readable while A is locked or rolls back a partial write.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        hold = sqlite3.connect(self.db_a, timeout=30, isolation_level=None)
        hold.execute("BEGIN EXCLUSIVE")
        try:
            with futures.ThreadPoolExecutor(max_workers=4) as pool:
                got = list(pool.map(
                    lambda u: self._get(self.opener_b, u)[0],
                    [f"{self.url_b}/project/1/papers/"] * 4))
            self.assertTrue(all(code == 200 for code in got))
        finally:
            hold.execute("ROLLBACK")
            hold.close()
        out = self._run_script(self.db_a, _FAIL_SCRIPT)
        self.assertIn("papers=1", out)
        with futures.ThreadPoolExecutor(max_workers=4) as pool:
            got = list(pool.map(lambda u: self._get(self.opener_b, u)[0],
                                [f"{self.url_b}/project/1/papers/"] * 2))
        self.assertTrue(all(code == 200 for code in got))
        self.assertEqual(len(self._paper_rows(self.db_a)), 1)
        self.assertEqual(len(self._paper_rows(self.db_b)), 1)

    # --- Real Set-Cookie names/paths never collide; sessions stay out of research DBs ---
    def test_cookies_and_no_session_rows(self) -> None:
        """
        Scope worker cookies independently and keep session rows out of stores.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """

        def names(url, opener):
            """
            Collect Set-Cookie headers from a worker's title-screening page.

            Args:
                url (str): Worker base URL including its UUID/epoch prefix.
                opener (urllib.request.OpenerDirector): Cookie-aware opener.

            Kwargs:
                None

            Returns:
                str: Semicolon-separated Set-Cookie header values.
            """
            _, _, headers = self._get(opener, url + "/project/1/screen/title/")
            return "; ".join(headers)

        got_a = names(self.url_a, self.opener_a)
        got_b = names(self.url_b, self.opener_b)
        short_a, short_b = UUID(self.uuid_a).hex[:8], UUID(self.uuid_b).hex[:8]
        # ponytail: the CSRF cookie is the live browser proof (session cookies
        # only appear once a session is touched); session names come from the
        # real worker settings asserted below.
        self.assertIn(f"wpcsrft_{short_a}", str(list(self.jar_a)))
        self.assertIn(f"wpcsrft_{short_b}", str(list(self.jar_b)))
        self.assertNotIn(f"wpcsrft_{short_a}", str(list(self.jar_b)))
        self.assertNotIn(f"wpcsrft_{short_b}", str(list(self.jar_a)))
        self.assertRegex(
            got_a, rf"Path=/p/{re.escape(self.uuid_a)}/{re.escape(self.epoch_a)}/")
        self.assertRegex(
            got_b, rf"Path=/p/{re.escape(self.uuid_b)}/{re.escape(self.epoch_b)}/")
        for db, store_uuid, epoch in ((self.db_a, self.uuid_a, self.epoch_a),
                                      (self.db_b, self.uuid_b, self.epoch_b)):
            out = self._run_script(db, _SETTINGS_SCRIPT, extra_env={
                "WP_ROLE": "worker", "WP_PROJECT_UUID": store_uuid,
                "WP_WORKER_EPOCH": epoch})
            self.assertIn(
                "ENGINE=django.contrib.sessions.backends.signed_cookies", out)
            self.assertIn(f"SESSION=wpsess_{UUID(store_uuid).hex[:8]}", out)
            self.assertEqual(self._table_count(db, "django_session"), 0)
        out = self._run_script(
            self.db_a, "import os\nprint('ROLE='+os.getenv('WP_ROLE',''))\n",
            extra_env={"WP_ROLE": "manager"})
        self.assertIn("ROLE=manager", out)
        with tempfile.TemporaryDirectory(prefix="wp0103-reg-") as tmp:
            registry = init_app_registry(Path(tmp) / "registry.sqlite3")
            with sqlite3.connect(registry) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({t for t in tables if t.startswith("literature_")})
        for db in (self.db_a, self.db_b):
            manifest = open_project_store(db.parent)
            self.assertEqual(str(manifest.project_uuid),
                             self.uuid_a if db == self.db_a else self.uuid_b)

    # --- Static guard: one DATABASES owner, default-alias transactions, signed cookies ---
    def test_static_worker_guards(self) -> None:
        """
        Check the sole DATABASES owner and the developer session backend.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        import re

        base = Path(settings.BASE_DIR)
        hits = []
        # ponytail: product code only; test files legitimately name DATABASES.
        sources = [base / "config" / "settings.py"]
        sources += [p for p in (base / "literature").glob("*.py")
                    if p.name != "tests"]
        sources += list((base / "literature" / "services").glob("*.py"))
        sources += list((base / "literature" / "providers").glob("*.py"))
        for path in sources:
            for line in path.read_text(encoding="utf-8").splitlines():
                if re.match(r"\s*DATABASES\s*(\[|=)", line):
                    hits.append(path.name)
        self.assertEqual(hits, ["settings.py"])
        # ponytail: signed cookies are scoped to manager/worker processes;
        # developer (this runner) keeps the legacy DB session backend.
        self.assertEqual(settings.SESSION_ENGINE,
                         "django.contrib.sessions.backends.db")

    # --- Row counter tolerant of pre-migration stores ---
    @staticmethod
    def _table_count(db: Path, table: str) -> int:
        """
        Count synthetic table rows, treating an absent table as empty.

        Args:
            db (Path): Synthetic SQLite store to inspect.
            table (str): Internal table name used directly in the count query.

        Kwargs:
            None

        Returns:
            int: Row count, or zero if the table is absent.
        """
        with sqlite3.connect(db) as connection:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if table not in names:
                return 0
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
