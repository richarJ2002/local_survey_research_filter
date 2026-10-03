#!/usr/bin/env python3

'''
@File:   test_wp0105_lifecycle.py

@Brief:  WP01.05: tab-scoped open handles, explicit Exit, recovery (synthetic).

@Date:   03/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import http.cookiejar
import os
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

# Custom library imports
from django.conf import settings as dj_settings
from django.test import Client, SimpleTestCase

from literature import workspace as ws
from literature.project_store import (
    app_registry_path,
    create_project_store,
    init_app_registry,
    open_project_store,
)

# ------------------------------------------------------------------------ #
# Small same-origin HTTP client (cookies + worker CSRF, stdlib only)
# ------------------------------------------------------------------------ #


class _Client:
    """Minimal cookie/CSRF client for real worker HTTP (synthetic stores)."""

    def __init__(self, handle: str | None = None) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.handle = handle

    def get(self, url: str, timeout: float = 20.0):
        request = urllib.request.Request(url, method="GET")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers
        except (urllib.error.URLError, ConnectionError, OSError):
            # ponytail: a stopped worker's dead port is as safe as a 404;
            # nothing there can accept a write.
            return "REFUSED", b"", {}

    def post(self, url: str, fields: dict, timeout: float = 30.0):
        body_fields = dict(fields)
        # ponytail: lifecycle paths validate handle_id themselves; every
        # other worker mutation carries the tab handle for middleware.
        if self.handle and "/lifecycle/" not in url and "wp_handle" not in body_fields:
            body_fields["wp_handle"] = self.handle
        data = urllib.parse.urlencode(body_fields, doseq=True).encode()
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "X-CSRFToken": self._csrf()})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers
        except (urllib.error.URLError, ConnectionError, OSError):
            return "REFUSED", b"", {}

    def post_json(self, url: str, payload: dict, timeout: float = 30.0):
        import json as _json

        data = _json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "X-CSRFToken": self._csrf()}
        if self.handle:
            headers["X-Project-Handle"] = self.handle
        request = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers
        except (urllib.error.URLError, ConnectionError, OSError):
            return "REFUSED", b"", {}

    def _csrf(self) -> str:
        for cookie in self.jar:
            # ponytail: exact worker cookie only; no global csrftoken fallback.
            if cookie.name.startswith("wpcsrft_"):
                return cookie.value
        return ""


# ------------------------------------------------------------------------ #
# Proof tests (synthetic temp dirs/stores only; live DB never written)
# ------------------------------------------------------------------------ #


class LifecycleTests(SimpleTestCase):
    """Tab handles share workers A/A, isolate A/B, exit singly, recover."""

    _canonicals: ClassVar[list[str]] = []
    _handles: ClassVar[list[str]] = []

    # --- Isolated XDG state plus two synthetic stores, once ---
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory(prefix="wp0105-lc-")
        tmp = Path(cls._tmp.name)
        cls._old_xdg = (os.getenv("XDG_DATA_HOME"), os.getenv("XDG_CACHE_HOME"))
        os.environ["XDG_DATA_HOME"] = str(tmp / "xdg-data")
        os.environ["XDG_CACHE_HOME"] = str(tmp / "xdg-cache")
        live = Path(dj_settings.BASE_DIR) / "projects" / "db.sqlite3"
        cls._live_mtime = live.stat().st_mtime if live.exists() else None
        cls._canonicals = []
        cls._handles = []
        cls.store_a = create_project_store(tmp / "projA", "Lifecycle A")
        cls.store_b = create_project_store(tmp / "projB", "Lifecycle B")
        cls._old_role = os.getenv("WP_ROLE")
        os.environ.pop("WP_ROLE", None)

    # --- Release handles, stop ONLY owned children, prove live DB untouched ---
    @classmethod
    def tearDownClass(cls):
        for handle in list(cls._handles):
            try:
                ws.release_handle(handle)
            except (OSError, ValueError):
                pass
        try:
            ws.sweep_idle(lease_ttl=0, worker_grace=0)
        except (OSError, ValueError):
            pass
        for canonical in list(cls._canonicals):
            # ponytail: Popen identity only; a bare registry pid is never signalled.
            proc = ws._CHILDREN.get(canonical)
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except OSError:
                    pass
            ws.forget_worker(canonical)
            ws._CHILDREN.pop(canonical, None)
        if cls._old_role is None:
            os.environ.pop("WP_ROLE", None)
        else:
            os.environ["WP_ROLE"] = cls._old_role
        for key, value in zip(("XDG_DATA_HOME", "XDG_CACHE_HOME"), cls._old_xdg):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()
        live = Path(dj_settings.BASE_DIR) / "projects" / "db.sqlite3"
        if cls._live_mtime is not None and live.exists():
            assert live.stat().st_mtime == cls._live_mtime, "live DB touched!"
        super().tearDownClass()

    # --- Track one tab handle for teardown ---
    def _tab(self, directory: Path | str) -> dict:
        handle = ws.acquire_handle(directory)
        type(self)._handles.append(handle["handle_id"])
        # ponytail: per-test cleanup keeps a failed assertion from leaking a
        # lease into later tests; the second release is a harmless no-op.
        self.addCleanup(ws.release_handle, handle["handle_id"])
        if handle["store_canonical"] not in type(self)._canonicals:
            type(self)._canonicals.append(handle["store_canonical"])
        return handle

    # --- Forget one tracked handle after explicit release ---
    def _exit(self, handle: dict) -> None:
        self.assertTrue(ws.release_handle(handle["handle_id"]))
        try:
            type(self)._handles.remove(handle["handle_id"])
        except ValueError:
            pass

    # --- Papers URL for a handle ---
    @staticmethod
    def _papers(handle: dict) -> str:
        return f"{handle['base']}{handle['prefix']}/project/{handle['project_id']}/papers/"

    # --- A/A tabs share one worker; exiting one leaves the other active ---
    def test_aa_tabs_share_worker_exit_one_keeps_other(self) -> None:
        tab1 = self._tab(self.store_a.location)
        tab2 = self._tab(self.store_a.location)
        self.assertNotEqual(tab1["handle_id"], tab2["handle_id"])
        # ponytail: same store reuses the worker; distinct handles per tab.
        self.assertEqual(tab2["port"], tab1["port"])
        self.assertEqual(tab2["epoch"], tab1["epoch"])
        self.assertTrue(tab2["reused"])
        self.assertEqual(ws.live_handle_count(tab1["store_canonical"]), 2)
        client = _Client()
        for tab in (tab1, tab2):
            status, body, _ = client.get(self._papers(tab))
            self.assertEqual(status, 200)
            self.assertIn("Lifecycle A", body.decode())
        self._exit(tab1)
        self.assertEqual(ws.live_handle_count(tab2["store_canonical"]), 1)
        self.assertIsNotNone(ws.heartbeat_handle(tab2["handle_id"]))
        status, body, _ = client.get(self._papers(tab2))
        self.assertEqual(status, 200)
        self.assertIn("Lifecycle A", body.decode())
        self._exit(tab2)
        self.assertEqual(ws.live_handle_count(tab1["store_canonical"]), 0)

    # --- A/B tabs stay isolated under concurrent HTTP ---
    def test_ab_tabs_isolated_concurrent(self) -> None:
        tab_a = self._tab(self.store_a.location)
        tab_b = self._tab(self.store_b.location)
        self.assertNotEqual(tab_a["prefix"], tab_b["prefix"])
        self.assertEqual(ws.live_handle_count(tab_a["store_canonical"]), 1)
        self.assertEqual(ws.live_handle_count(tab_b["store_canonical"]), 1)

        def fetch(handle: dict):
            status, body, _ = _Client().get(self._papers(handle))
            return status, body.decode()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures_a = [pool.submit(fetch, tab_a) for _ in range(4)]
            futures_b = [pool.submit(fetch, tab_b) for _ in range(4)]
            for future in futures_a:
                status, body = future.result(timeout=30)
                self.assertEqual(status, 200)
                self.assertIn("Lifecycle A", body)
                self.assertNotIn("Lifecycle B", body)
            for future in futures_b:
                status, body = future.result(timeout=30)
                self.assertEqual(status, 200)
                self.assertIn("Lifecycle B", body)
                self.assertNotIn("Lifecycle A", body)
        self._exit(tab_a)
        self._exit(tab_b)

    # --- Stale prefixes/ports fail closed and never write across stores ---
    def test_stale_handle_never_follows_port_to_another_store(self) -> None:
        tab_a = self._tab(self.store_a.location)
        tab_b = self._tab(self.store_b.location)
        old_prefix = f"{tab_a['base']}{tab_a['prefix']}"
        old_handle = tab_a["handle_id"]
        old_pid, old_port = tab_a["pid"], tab_a["port"]
        self.assertGreater(old_pid, 0)
        before_b = self._search_count(self.store_b.location)
        client = _Client()
        # ponytail: a B prefix on A's port is a wrong-UUID write attempt.
        uuid_b = tab_b["prefix"].split("/")[2]
        status, _, _ = client.post(
            f"http://127.0.0.1:{old_port}/p/{uuid_b}/{tab_b['epoch']}"
            f"/project/{tab_b['project_id']}/search/new/",
            {"name": "Cross-store probe", "keywords_json": '["x"]'})
        self.assertEqual(status, 404, "cross-store write must fail closed")
        self.assertEqual(self._search_count(self.store_b.location), before_b)
        self._exit(tab_a)
        self._exit(tab_b)
        # ponytail: restart moves the epoch; the old handle/prefix must not follow.
        self.assertTrue(ws.stop_worker(str(self.store_a.location))["stopped"])
        fresh = self._tab(self.store_a.location)
        self.assertNotEqual(fresh["epoch"], tab_a["epoch"])
        self.assertIsNone(ws.heartbeat_handle(old_handle))
        status, _, _ = client.get(f"{old_prefix}/project/{tab_a['project_id']}/papers/")
        # ponytail: dead port (REFUSED) or live-but-wrong epoch (404) both fail
        # closed; neither can take a write for the reopened store.
        self.assertIn(status, (404, "REFUSED"))
        status, _, _ = client.post(
            f"{old_prefix}/project/{tab_a['project_id']}/search/new/",
            {"name": "Stale probe", "keywords_json": '["x"]'})
        self.assertIn(status, (404, "REFUSED"))
        self._exit(fresh)
        self.assertTrue(ws.stop_worker(str(self.store_a.location))["stopped"])

    # --- Reload keeps the handle; crash re-opens deliberately; beacons unused ---
    def test_reload_crash_and_unload_hint(self) -> None:
        tab = self._tab(self.store_a.location)
        self.assertIsNotNone(ws.heartbeat_handle(tab["handle_id"]))
        # ponytail: reload is the same sessionStorage handle heartbeat again.
        self.assertIsNotNone(ws.heartbeat_handle(tab["handle_id"]))
        client = _Client()
        status, _, _ = client.get(self._papers(tab))
        self.assertEqual(status, 200)
        # ponytail: crash drops the client handle without release; the worker
        # stays up and a deliberate re-open takes a second lease.
        reopened = self._tab(self.store_a.location)
        self.assertEqual(ws.live_handle_count(tab["store_canonical"]), 2)
        self.assertIsNotNone(ws.heartbeat_handle(reopened["handle_id"]))
        # ponytail: unload sends no beacon; prove the script registers no
        # pagehide/unload release handler and never uses sendBeacon (the only
        # unload listener is the dirty-draft beforeunload guard).
        script = (Path(dj_settings.BASE_DIR) / "literature" / "static"
                  / "literature" / "project_handle.js").read_text(encoding="utf-8")
        self.assertNotIn("sendBeacon", script)
        self.assertNotIn('addEventListener("pagehide"', script)
        self.assertNotIn('addEventListener("unload"', script)
        self._exit(tab)
        self._exit(reopened)

    # --- Bounded idle sweep expires leases but never kills a held worker ---
    def test_idle_sweep_bounded_never_kills_live_handles(self) -> None:
        tab = self._tab(self.store_a.location)
        refused = ws.stop_worker(str(self.store_a.location))
        self.assertFalse(refused["stopped"])
        self.assertEqual(refused["reason"], "handles-live")
        client = _Client()
        status, _, _ = client.get(self._papers(tab))
        self.assertEqual(status, 200)
        swept = ws.sweep_idle(lease_ttl=3600, worker_grace=3600)
        self.assertEqual(swept["expired"], 0)
        self.assertNotIn(str(self.store_a.location), swept["stopped"])
        self._exit(tab)
        swept = ws.sweep_idle(lease_ttl=0, worker_grace=0)
        self.assertIn(str(self.store_a.location), swept["stopped"])
        self.assertIsNone(ws.find_worker(str(self.store_a.location)))

    # --- Committed records survive worker restart ---
    def test_committed_search_survives_restart(self) -> None:
        tab = self._tab(self.store_a.location)
        client = _Client(handle=tab["handle_id"])
        status, _, _ = client.get(self._papers(tab))
        self.assertEqual(status, 200)
        created = self._post_search(
            client, tab, "Restart Probe ws0105-a")
        self.assertTrue(created)
        self.assertEqual(
            self._search_count(self.store_a.location, "Restart Probe ws0105-a"), 1)
        self._exit(tab)
        self.assertTrue(ws.stop_worker(str(self.store_a.location))["stopped"])
        again = self._tab(self.store_a.location)
        self.assertEqual(
            self._search_count(self.store_a.location, "Restart Probe ws0105-a"), 1)
        client2 = _Client()
        status, body, _ = client2.get(self._papers(again))
        self.assertEqual(status, 200)
        self.assertIn("Lifecycle A", body.decode())
        self._exit(again)
        self.assertTrue(ws.stop_worker(str(self.store_a.location))["stopped"])

    # --- Drain finishes the admitted write before any shutdown signal ---
    def test_drain_finishes_admitted_write_before_stop(self) -> None:
        import json as _json

        with tempfile.TemporaryDirectory(prefix="wp0105-drain-") as tmp:
            manifest = create_project_store(Path(tmp) / "draining", "Drain Probe")
            canonical = str(manifest.location)
            tab = self._tab(manifest.location)
            db_path = manifest.location / "project.sqlite3"
            release = threading.Event()
            held = threading.Event()

            def hold_exclusive() -> None:
                connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
                try:
                    connection.execute("BEGIN EXCLUSIVE")
                    connection.execute(
                        "UPDATE literature_project SET name = name WHERE id = ?",
                        (tab["project_id"],))
                    held.set()
                    release.wait(timeout=15)
                    connection.rollback()
                finally:
                    connection.close()

            base = f"{tab['base']}{tab['prefix']}/project/{tab['project_id']}"
            writer = _Client(handle=tab["handle_id"])
            status, _, _ = writer.get(self._papers(tab))
            self.assertEqual(status, 200)
            # ponytail: warm the worker CSRF cookie from the real form page
            # before the lock; the papers list plants no cookie itself.
            status, _, _ = writer.get(f"{base}/search/new/")
            self.assertEqual(status, 200)
            locker = threading.Thread(target=hold_exclusive, daemon=True)
            locker.start()
            # ponytail: the POST must arrive while the lock is held, or it
            # commits immediately and there is nothing admitted to drain.
            self.assertTrue(held.wait(timeout=15), "lock never held")
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        writer.post,
                        f"{base}/search/new/",
                        {"name": "Drain Probe ws0105", "keywords_json": '["drain"]',
                         "venues_json": "[]", "author_names_json": "[]"})
                    # ponytail: the admitted mutation is inside the view
                    # (counter 1) before the drain starts, never after.
                    self.assertTrue(
                        self._wait_in_flight(tab, 1, timeout=15),
                        "worker never admitted the write")
                    status, body, _ = writer.post(
                        f"{base}/lifecycle/drain/", {"command": "start"})
                    self.assertEqual(status, 200, body[:300])
                    self.assertGreaterEqual(
                        int(_json.loads(body.decode())["in_flight"]), 1)
                    # ponytail: new work with a VALID handle is refused while
                    # draining. The zero-write proof follows after release:
                    # no read may touch the exclusively locked DB here, or
                    # the observation itself blocks past every timeout.
                    status, _, _ = writer.post(
                        f"{base}/search/new/",
                        {"name": "Rejected ws0105", "keywords_json": '["x"]',
                         "venues_json": "[]", "author_names_json": "[]"})
                    self.assertEqual(status, 409)
                    # ponytail: releasing the lock lets the ADMITTED write
                    # commit whole before any signal; the row proves the finish.
                    release.set()
                    status, _, _ = pending.result(timeout=40)
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        self._search_count(manifest.location, "Drain Probe ws0105"), 1)
                    self.assertEqual(
                        self._search_count(manifest.location, "Rejected ws0105"), 0)
                    self.assertTrue(self._wait_in_flight(tab, 0, timeout=20))
                    self._exit(tab)
                    result = ws.drain_and_stop(canonical, in_flight_timeout=20.0)
                    self.assertTrue(result["stopped"], result)
                open_project_store(manifest.location)
                with sqlite3.connect(db_path) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(
                        list(connection.execute("PRAGMA foreign_key_check")), [])
            finally:
                release.set()
                locker.join(timeout=15)
            open_project_store(manifest.location)

    # --- Poll the worker drain counter until it shows the wanted count ---
    def _wait_in_flight(self, handle: dict, want: int, timeout: float = 15.0) -> bool:
        import json as _json
        import time as _time

        probe = _Client()
        base = f"{handle['base']}{handle['prefix']}/project/{handle['project_id']}"
        end = _time.time() + timeout
        while _time.time() < end:
            status, body, _ = probe.get(f"{base}/lifecycle/drain/")
            if status == 200:
                try:
                    if int(_json.loads(body.decode())["in_flight"]) == want:
                        return True
                except (ValueError, KeyError):
                    pass
            _time.sleep(0.2)
        return False

    # --- Manager stays research-free; cookies never form a global session ---
    def test_manager_and_cookie_scoping(self) -> None:
        tab_a = self._tab(self.store_a.location)
        tab_b = self._tab(self.store_b.location)
        old = os.getenv("WP_ROLE")
        os.environ["WP_ROLE"] = "manager"
        try:
            manager = Client(enforce_csrf_checks=True)
            for path in ("/project/1/", "/project/1/papers/",
                         "/project/1/lifecycle/status/?handle_id=x"):
                self.assertEqual(manager.get(path).status_code, 404, path)
            self.assertEqual(manager.get("/settings/").status_code, 200)
            manager.get("/")
            token = manager.cookies["csrftoken"].value
            response = manager.post(
                "/workspace/open/", {"directory": str(self.store_a.location)},
                HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 302)
            # ponytail: the handoff carries the explicit Exit hop, not a cookie.
            self.assertIn("?manager=http", response["Location"])
            self.assertIn(quote("http://", safe=""), response["Location"])
        finally:
            if old is None:
                os.environ.pop("WP_ROLE", None)
            else:
                os.environ["WP_ROLE"] = old
        short_a = tab_a["prefix"].split("/")[2].replace("-", "")[:8]
        short_b = tab_b["prefix"].split("/")[2].replace("-", "")[:8]
        self.assertNotEqual(short_a, short_b)
        joined_parts: list[str] = []
        for short, tab in ((short_a, tab_a), (short_b, tab_b)):
            status_url = (f"{tab['base']}{tab['prefix']}/project/"
                          f"{tab['project_id']}/lifecycle/status/"
                          f"?handle_id={tab['handle_id']}")
            probe = _Client()
            status, _, headers = probe.get(status_url)
            self.assertEqual(status, 200)
            cookies = headers.get_all("Set-Cookie") or []
            self.assertTrue(
                any(f"wpcsrft_{short}" in c and f"Path={tab['prefix']}/" in c
                    for c in cookies), cookies)
            joined_parts.append("\n".join(cookies))
        joined = "\n".join(joined_parts)
        self.assertNotIn("sessionid=", joined)
        self.assertNotIn("csrftoken=", joined.replace("wpcsrft_", ""))
        self._exit(tab_a)
        self._exit(tab_b)

    # --- Worker lifecycle HTTP: attach/heartbeat/release/status round-trip ---
    def test_lifecycle_http_round_trip(self) -> None:
        import json as _json

        tab = self._tab(self.store_a.location)
        client = _Client()
        base = f"{tab['base']}{tab['prefix']}/project/{tab['project_id']}"
        status, _, _ = client.get(f"{base}/lifecycle/status/?handle_id=nope")
        # ponytail: status GET also plants the path-scoped worker CSRF cookie.
        self.assertEqual(status, 410)
        status, body, _ = client.post(
            f"{base}/lifecycle/attach/", {"nav": "navigate"})
        self.assertEqual(status, 200)

        attached = _json.loads(body.decode())
        handle_id = attached["handle_id"]
        self.assertFalse(attached["reused"])
        self.assertTrue(attached["tab_token"])
        type(self)._handles.append(handle_id)
        try:
            status, _, _ = client.get(f"{base}/lifecycle/status/?handle_id={handle_id}")
            self.assertEqual(status, 200)
            status, _, _ = client.post(
                f"{base}/lifecycle/heartbeat/", {"handle_id": handle_id})
            self.assertEqual(status, 200)
            status, _, _ = client.post(
                f"{base}/lifecycle/release/", {"handle_id": handle_id})
            self.assertEqual(status, 200)
            status, _, _ = client.get(f"{base}/lifecycle/status/?handle_id={handle_id}")
            self.assertEqual(status, 410)
            status, _, _ = client.post(
                f"{base}/lifecycle/heartbeat/", {"handle_id": handle_id})
            self.assertEqual(status, 410)
        finally:
            try:
                type(self)._handles.remove(handle_id)
            except ValueError:
                pass
        self._exit(tab)

    # --- Released/foreign/missing handles cannot mutate; zero writes ---
    def test_mutations_require_live_handle(self) -> None:
        tab_a = self._tab(self.store_a.location)
        tab_b = self._tab(self.store_b.location)
        search_url_a = (f"{tab_a['base']}{tab_a['prefix']}/project/"
                        f"{tab_a['project_id']}/search/new/")
        decide_url_a = f"{tab_a['base']}{tab_a['prefix']}/screening/decide/"
        fields = {"name": "Handle Probe ws0105", "keywords_json": '["h"]',
                  "venues_json": "[]", "author_names_json": "[]"}
        before = self._search_count(self.store_a.location)
        owned = _Client(handle=tab_a["handle_id"])
        status, _, _ = owned.get(search_url_a)
        self.assertEqual(status, 200)
        # ponytail: the owned tab writes fine (redirect followed to 200).
        status, _, _ = owned.post(search_url_a, dict(fields))
        self.assertEqual(status, 200)
        self.assertEqual(self._search_count(self.store_a.location), before + 1)
        # ponytail: JSON decide with a live handle reaches the view (404 for
        # the absent paper proves middleware passed, not the 410 gate).
        status, _, _ = owned.post_json(
            decide_url_a, {"paper_id": 9999, "stage_id": 9999, "decision": "ACCEPT"})
        self.assertEqual(status, 404)
        for probe in (_Client(), _Client(handle="hdeadbeef00"),
                      _Client(handle=tab_b["handle_id"])):
            # ponytail: missing, garbage and foreign-store handles all fail
            # closed with no DB touch.
            status, _, _ = probe.post(search_url_a, dict(fields))
            self.assertEqual(status, 410, status)
            status, _, _ = probe.post_json(
                decide_url_a, {"paper_id": 9999, "stage_id": 9999, "decision": "ACCEPT"})
            self.assertEqual(status, 410, status)
        self.assertEqual(self._search_count(self.store_a.location), before + 1)
        # ponytail: after explicit Exit the same handle cannot write again.
        self._exit(tab_a)
        status, _, _ = owned.post(search_url_a, dict(fields))
        self.assertEqual(status, 410)
        self.assertEqual(self._search_count(self.store_a.location), before + 1)
        self._exit(tab_b)

    # --- Duplicate tabs mint their own handle; Exit never releases survivors ---
    def test_attach_duplicate_mints_survivor_safe(self) -> None:
        tab = self._tab(self.store_a.location)
        canonical = tab["store_canonical"]
        first = ws.attach_lease(
            canonical, tab["project_uuid"], tab["epoch"], tab["port"],
            None, None, "navigate")
        self.assertFalse(first["reused"])
        type(self)._handles.append(first["handle_id"])
        try:
            kept = ws.attach_lease(
                canonical, tab["project_uuid"], tab["epoch"], tab["port"],
                first["handle_id"], first["tab_token"], "reload")
            self.assertTrue(kept["reused"])
            self.assertEqual(kept["handle_id"], first["handle_id"])
            # ponytail: source heartbeats, so this same-token navigate is a
            # duplicated tab and must mint, not share.
            self.assertIsNotNone(ws.heartbeat_handle(first["handle_id"]))
            duplicate = ws.attach_lease(
                canonical, tab["project_uuid"], tab["epoch"], tab["port"],
                first["handle_id"], first["tab_token"], "navigate")
            self.assertFalse(duplicate["reused"])
            self.assertNotEqual(duplicate["handle_id"], first["handle_id"])
            type(self)._handles.append(duplicate["handle_id"])
            try:
                ws.release_handle(first["handle_id"])
                # ponytail: exiting the source leaves the survivor fully valid.
                self.assertIsNotNone(ws.heartbeat_handle(duplicate["handle_id"]))
                self.assertEqual(ws.live_handle_count(canonical), 2)
                ws.release_handle(duplicate["handle_id"])
            finally:
                try:
                    type(self)._handles.remove(duplicate["handle_id"])
                except ValueError:
                    pass
            # ponytail: a long-idle source means same-tab navigation: keep.
            third = ws.attach_lease(
                canonical, tab["project_uuid"], tab["epoch"], tab["port"],
                None, None, "navigate")
            type(self)._handles.append(third["handle_id"])
            try:
                registry = init_app_registry(app_registry_path())
                with sqlite3.connect(registry) as connection:
                    connection.execute(
                        "UPDATE leases SET last_seen = ? WHERE handle_id = ?",
                        ("2000-01-01T00:00:00+00:00", third["handle_id"]))
                    connection.commit()
                kept_nav = ws.attach_lease(
                    canonical, tab["project_uuid"], tab["epoch"], tab["port"],
                    third["handle_id"], third["tab_token"], "navigate")
                self.assertTrue(kept_nav["reused"])
                self.assertEqual(kept_nav["handle_id"], third["handle_id"])
                ws.release_handle(third["handle_id"])
            finally:
                try:
                    type(self)._handles.remove(third["handle_id"])
                except ValueError:
                    pass
        finally:
            try:
                type(self)._handles.remove(first["handle_id"])
            except ValueError:
                pass
        self._exit(tab)

    # --- Concurrent first-openers elect exactly one worker process/epoch ---
    def test_concurrent_acquire_single_worker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0105-race-") as tmp:
            manifest = create_project_store(Path(tmp) / "racy", "Race Probe")
            canonical = str(manifest.location)
            barrier = threading.Barrier(8)
            results: list[dict] = []

            def opener() -> None:
                barrier.wait(timeout=30)
                results.append(ws.acquire_handle(manifest.location))

            threads = [threading.Thread(target=opener) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
            self.assertEqual(len(results), 8)
            epochs = {entry["epoch"] for entry in results}
            ports = {entry["port"] for entry in results}
            handles = [entry["handle_id"] for entry in results]
            # ponytail: one process/epoch serves all eight tabs, each owned.
            self.assertEqual(len(epochs), 1)
            self.assertEqual(len(ports), 1)
            self.assertEqual(len(set(handles)), 8)
            self.assertEqual(ws.live_handle_count(canonical), 8)
            for entry in results:
                type(self)._handles.append(entry["handle_id"])
                self.addCleanup(ws.release_handle, entry["handle_id"])
            if canonical not in type(self)._canonicals:
                type(self)._canonicals.append(canonical)
            client = _Client()
            status, body, _ = client.get(
                f"{results[0]['base']}{results[0]['prefix']}/project/"
                f"{results[0]['project_id']}/papers/")
            self.assertEqual(status, 200)
            self.assertIn("Race Probe", body.decode())
            for entry in results:
                ws.release_handle(entry["handle_id"])
            self.assertTrue(ws.stop_worker(canonical)["stopped"])

    # --- A racing stop never kills a worker out from under a valid lease ---
    def test_stop_vs_acquire_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0105-stoprace-") as tmp:
            manifest = create_project_store(Path(tmp) / "stoprace", "Stop Race")
            canonical = str(manifest.location)
            first = self._tab(manifest.location)
            barrier = threading.Barrier(2)
            outcomes: dict = {}

            def stopper() -> None:
                barrier.wait(timeout=30)
                outcomes["stop"] = ws.stop_worker(canonical)

            def opener() -> None:
                barrier.wait(timeout=30)
                outcomes["open"] = ws.acquire_handle(manifest.location)

            stop_thread = threading.Thread(target=stopper)
            open_thread = threading.Thread(target=opener)
            stop_thread.start()
            open_thread.start()
            stop_thread.join(timeout=120)
            open_thread.join(timeout=120)
            opened = outcomes["open"]
            type(self)._handles.append(opened["handle_id"])
            self.addCleanup(ws.release_handle, opened["handle_id"])
            if canonical not in type(self)._canonicals:
                type(self)._canonicals.append(canonical)
            recorded = ws.find_worker(canonical)
            # ponytail: exactly one recorded worker answers afterwards; every
            # surviving lease belongs to it, the rest fail cleanly for re-open.
            # Dead-epoch lease rows linger for the TTL sweep, so only live
            # heartbeats count here, never raw row totals.
            self.assertIsNotNone(recorded)
            assert recorded is not None
            self.assertIn("stopped", outcomes["stop"])
            final_epoch = str(recorded["worker_epoch"])
            alive = 0
            for entry in (first, opened):
                if ws.heartbeat_handle(entry["handle_id"]) is not None:
                    alive += 1
                    self.assertEqual(entry["epoch"], final_epoch)
                else:
                    self.assertNotEqual(entry["epoch"], final_epoch)
            client = _Client()
            status, _, _ = client.get(
                f"http://127.0.0.1:{recorded['port']}/p/{opened['project_uuid']}/"
                f"{final_epoch}/project/{opened['project_id']}/papers/")
            if alive:
                self.assertEqual(status, 200)
            for entry in (first, opened):
                ws.release_handle(entry["handle_id"])
            if ws.find_worker(canonical) is not None:
                ws.drain_and_stop(canonical, in_flight_timeout=5.0)

    # --- Worker GETs never bootstrap stages; legacy developer path keeps it ---
    def test_worker_get_never_writes_stages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0105-stages-") as tmp:
            manifest = create_project_store(Path(tmp) / "stages", "Stage Probe")
            canonical = str(manifest.location)
            db_path = manifest.location / "project.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("DELETE FROM literature_stage")
                connection.commit()
            tab = self._tab(manifest.location)
            client = _Client()
            detail = (f"{tab['base']}{tab['prefix']}/project/{tab['project_id']}")
            for path in ("", "/papers/"):
                status, _, _ = client.get(detail + path)
                self.assertEqual(status, 200, path)
            # ponytail: a stageless store has no screening round; the worker
            # fails that closed (404) instead of bootstrapping on a GET.
            status, _, _ = client.get(detail + "/screen/title/")
            self.assertEqual(status, 404)
            with sqlite3.connect(db_path) as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM literature_stage").fetchone()[0]
            # ponytail: the worker rendered stageless pages without writing;
            # stage bootstrap stays owned by creation, not by safe methods.
            self.assertEqual(remaining, 0)
            self._exit(tab)
            self.assertTrue(ws.stop_worker(canonical)["stopped"])
    def _post_search(self, client: _Client, handle: dict, name: str) -> bool:
        url = (f"{handle['base']}{handle['prefix']}/project/"
               f"{handle['project_id']}/search/new/")
        status, _, _ = client.get(url)
        if status != 200:
            return False
        status, _, _ = client.post(url, {
            "name": name,
            "keywords_json": '["lifecycle probe"]',
            "venues_json": "[]",
            "author_names_json": "[]",
        })
        # ponytail: the opener follows the prefixed redirect; success is the
        # committed row, not the status code alone.
        return self._search_count(
            Path(handle["store_canonical"]), name) == 1

    # --- Count searches by name with a read-only stdlib connection ---
    @staticmethod
    def _search_count(location: Path | str, name: str | None = None) -> int:
        db = Path(location) / "project.sqlite3"
        uri = db.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
            if name is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM literature_search").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM literature_search WHERE name = ?",
                    (name,)).fetchone()
        return int(row[0])
