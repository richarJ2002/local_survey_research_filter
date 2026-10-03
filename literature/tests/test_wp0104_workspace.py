#!/usr/bin/env python3

'''
@File:   test_wp0104_workspace.py

@Brief:  WP01.04: New/Load workspace UX over real HTTP (synthetic stores).

@Date:   03/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import tempfile
import urllib.request
from pathlib import Path
from typing import ClassVar
from unittest import skipIf

# Custom library imports
from django.conf import settings as dj_settings
from django.test import Client, SimpleTestCase

from literature import workspace as ws
from literature.project_store import (
    ProjectStoreError,
    create_project_store,
    inspect_project_store,
    open_project_store,
)

# ------------------------------------------------------------------------ #
# Proof tests (synthetic temp dirs/stores only; live DB never written)
# ------------------------------------------------------------------------ #


class WorkspaceTests(SimpleTestCase):
    """Manager landing + typed New/Load leading to real bound workers."""

    _workers: ClassVar[list[dict]] = []
    _old_role: str | None = None

    # --- Isolated XDG state plus a small synthetic workspace, once ---
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory(prefix="wp0104-ws-")
        tmp = Path(cls._tmp.name)
        cls._old_xdg = (os.getenv("XDG_DATA_HOME"), os.getenv("XDG_CACHE_HOME"))
        os.environ["XDG_DATA_HOME"] = str(tmp / "xdg-data")
        os.environ["XDG_CACHE_HOME"] = str(tmp / "xdg-cache")
        live = Path(dj_settings.BASE_DIR) / "projects" / "db.sqlite3"
        cls._live_mtime = live.stat().st_mtime if live.exists() else None
        cls.root = tmp / "workspace"
        cls.root.mkdir()
        cls.manifest_a = create_project_store(cls.root / "projA", "Project A")
        cls.manifest_b = create_project_store(cls.root / "projB", "Project B")
        cls._workers = []
        cls._old_role = os.getenv("WP_ROLE")

    # --- Stop owned workers, restore env, prove the live DB untouched ---
    @classmethod
    def tearDownClass(cls):
        for worker in cls._workers:
            try:
                pid = int(worker.get("pid", 0))
            except (ValueError, TypeError, AttributeError):
                continue
            # ponytail: never signal pid 0 (own process group) or negatives.
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        for worker in cls._workers:
            try:
                ws.forget_worker(worker["canonical"])
            except (OSError, ValueError, KeyError):
                pass
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

    # --- Run a block as the manager role (middleware reads env per request) ---
    def _as_manager(self):
        old = os.getenv("WP_ROLE")
        os.environ["WP_ROLE"] = "manager"
        self.addCleanup(lambda: os.environ.__setitem__("WP_ROLE", old) if old is not None
                        else os.environ.pop("WP_ROLE", None))
        return Client(enforce_csrf_checks=True)

    # --- Track spawned workers for teardown ---
    def _track(self, worker: dict, canonical: str) -> dict:
        worker["canonical"] = canonical
        self._workers.append(worker)
        type(self)._workers.append(worker)
        return worker

    # --- Only direct children (plus the root) are listed; never recursive ---
    def test_discovery_is_nonrecursive(self) -> None:
        (self.root / "notes.txt").write_text("not a project\n", encoding="utf-8")
        outer = self.root / "outer"
        outer.mkdir(exist_ok=True)
        create_project_store(outer / "deep", "Deep Project")
        found = ws.discover_workspace(self.root)
        dirs = {entry["directory"] for entry in found["entries"]}
        self.assertIn(str(self.manifest_a.location), dirs)
        self.assertIn(str(self.manifest_b.location), dirs)
        self.assertFalse(any(d.endswith("/deep") for d in dirs), dirs)
        self.assertFalse(any(d.endswith("/outer") for d in dirs), dirs)

    # --- Space/unicode names open exactly like plain ones ---
    def test_space_and_unicode_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0104-names-") as tmp:
            for name in ("my survey 2026", "étude über lowering"):
                created = create_project_store(Path(tmp) / name, f"Name {name}")
                reopened = open_project_store(Path(tmp) / name)
                self.assertEqual(reopened.project_uuid, created.project_uuid)
                self.assertTrue(inspect_project_store(Path(tmp) / name)["complete"])
            found = ws.discover_workspace(Path(tmp))
            self.assertEqual(len(found["entries"]), 2)
            self.assertEqual(found["errors"], [])

    # --- Outside-root symlink targets open; aliases collapse to one entry ---
    def test_external_symlink_and_alias_dedup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0104-link-") as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            created = create_project_store(outside / "real", "External")
            link = self.root / "linked-ext"
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(outside / "real", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable ({exc})")
            try:
                via_link = open_project_store(link)
                via_real = open_project_store(outside / "real")
                self.assertEqual(via_link.location, via_real.location)
                self.assertEqual(via_link.location, created.location)
                self.assertEqual(via_link.db_identity, via_real.db_identity)
                second = Path(tmp) / "ws2"
                second.mkdir()
                alias = second / "alias"
                alias.symlink_to(outside / "real", target_is_directory=True)
                found = ws.discover_workspace(second)
                self.assertEqual(len(found["entries"]), 1)
                # ponytail: one entry for two aliases; discovery counts the extra alias.
                both = Path(tmp) / "ws3"
                both.mkdir()
                (both / "one").symlink_to(outside / "real", target_is_directory=True)
                (both / "two").symlink_to(outside / "real", target_is_directory=True)
                merged = ws.discover_workspace(both)
                self.assertEqual(len(merged["entries"]), 1)
                self.assertEqual(merged["entries"][0]["aliases"], 1)
            finally:
                if link.is_symlink():
                    link.unlink()

    # --- Same UUID at different files is an explicit collision, never a merge ---
    def test_same_uuid_different_file_is_collision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0104-clash-") as tmp:
            base = Path(tmp) / "ws"
            base.mkdir()
            create_project_store(base / "first", "First")
            shutil.copytree(base / "first", base / "second", symlinks=True)
            found = ws.discover_workspace(base)
            flagged = [e for e in found["entries"] if e["collision"]]
            # ponytail: both copies share one UUID at different files; both flagged.
            self.assertEqual(len(flagged), 2)
            for entry in flagged:
                opened = open_project_store(entry["directory"])
                self.assertEqual(str(opened.project_uuid), entry["project_uuid"])

    # --- Refusals never mutate: populated New, missing Load, corrupt stores ---
    def test_refusals_never_mutate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0104-refuse-") as tmp:
            populated = Path(tmp) / "populated"
            populated.mkdir()
            (populated / "keep.txt").write_text("keep\n", encoding="utf-8")
            before = sorted(p.name for p in populated.iterdir())
            with self.assertRaises(ProjectStoreError):
                ws.check_new_target(str(populated))
            with self.assertRaises(ProjectStoreError):
                create_project_store(populated, "Nope")
            self.assertEqual(sorted(p.name for p in populated.iterdir()), before)
            missing = Path(tmp) / "absent"
            with self.assertRaises(ProjectStoreError):
                ws.check_load_target(str(missing))
            self.assertFalse(missing.exists())
            bad = Path(tmp) / "badjson"
            bad.mkdir()
            (bad / "project.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(ProjectStoreError):
                open_project_store(bad)
            self.assertEqual((bad / "project.json").read_text(encoding="utf-8"), "{not json")
            garbage = Path(tmp) / "garbage"
            garbage.mkdir()
            (garbage / "project.json").write_text(json.dumps({
                "format_version": 1,
                "project_uuid": "12345678-1234-5678-1234-567812345678",
                "database": "project.sqlite3",
            }), encoding="utf-8")
            (garbage / "project.sqlite3").write_bytes(b"not a database at all....")
            with self.assertRaises(ProjectStoreError):
                open_project_store(garbage)
            self.assertFalse(inspect_project_store(garbage)["complete"])

    # --- Unwritable destinations refuse with an actionable message ---
    @skipIf(os.geteuid() == 0, "root bypasses permission bits")
    def test_unwritable_destination_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0104-perm-") as tmp:
            locked = Path(tmp) / "locked"
            locked.mkdir(mode=0o500)
            try:
                with self.assertRaises(ProjectStoreError):
                    ws.check_new_target(str(locked / "child"))
            finally:
                locked.chmod(0o700)

    # --- Broken and looping links become error entries, never crashes ---
    def test_broken_and_loop_links_are_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0104-links-") as tmp:
            base = Path(tmp) / "ws"
            base.mkdir()
            create_project_store(base / "good", "Good")
            (base / "broken").symlink_to(base / "gone", target_is_directory=True)
            loop_a = base / "loopa"
            loop_b = base / "loopb"
            try:
                loop_a.symlink_to(loop_b, target_is_directory=True)
                loop_b.symlink_to(loop_a, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable ({exc})")
            found = ws.discover_workspace(base)
            self.assertTrue(any("broken" in e for e in found["errors"]), found)
            self.assertTrue(any("loop" in e for e in found["errors"]), found)
            self.assertEqual(len(found["entries"]), 1)

    # --- Manager landing renders the toggle; CSRF is really enforced ---
    def test_manager_landing_and_csrf(self) -> None:
        client = self._as_manager()
        ws.set_workspace_root(str(self.root))
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="workspace-mode"')
        self.assertContains(response, "New project folder")
        self.assertContains(response, "Existing project folder")
        self.assertContains(response, "Project A")
        self.assertContains(response, "csrfmiddlewaretoken")
        # ponytail: bare POST with no token proves CSRF is really enforced.
        bare = client.post("/workspace/set-root/", {"directory": str(self.root)})
        self.assertEqual(bare.status_code, 403)
        token = client.cookies["csrftoken"].value
        ok = client.post("/workspace/set-root/", {"directory": str(self.root)},
                         HTTP_X_CSRFTOKEN=token)
        self.assertEqual(ok.status_code, 302)
        # ponytail: research rows stay fail-closed behind the new landing.
        for path in ("/project/1/", "/project/1/papers/", "/screening/decide/"):
            self.assertEqual(client.get(path).status_code, 404, path)
        self.assertEqual(client.get("/settings/").status_code, 200)

    # --- Typed New leads over real HTTP to the correctly bound worker ---
    def test_new_flow_reaches_prefixed_worker(self) -> None:
        client = self._as_manager()
        ws.set_workspace_root(str(self.root))
        client.get("/")
        token = client.cookies["csrftoken"].value
        target = self.root / "typed new dir"
        response = client.post("/workspace/new/",
                               {"directory": str(target), "name": "Typed New"},
                               HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302, response.content[:500])
        location = response["Location"]
        self.assertIn(f"/p/{self._uuid_of(target)}/", location)
        self.assertTrue(location.startswith("http://127.0.0.1:"), location)
        canonical = str(target.resolve())
        recorded = ws.find_worker(canonical)
        self.assertIsNotNone(recorded)
        assert recorded is not None
        worker = self._track(
            {"pid": recorded["pid"], "port": recorded["port"]}, canonical)
        with urllib.request.urlopen(location, timeout=20) as page:
            body = page.read().decode()
            set_cookies = page.headers.get_all("Set-Cookie") or []
        self.assertEqual(page.status, 200)
        self.assertIn("Typed New", body)
        short = self._uuid_of(target).hex[:8]
        self.assertTrue(any(f"wpcsrft_{short}" in c for c in set_cookies), set_cookies)
        self.assertTrue(any("Path=/p/" in c for c in set_cookies), set_cookies)
        # ponytail: opening the same store again reuses the recorded worker.
        client.get("/")
        token = client.cookies["csrftoken"].value
        again = client.post("/workspace/open/", {"directory": str(target)},
                            HTTP_X_CSRFTOKEN=token)
        self.assertEqual(again.status_code, 302)
        self.assertTrue(again["Location"].startswith(
            f"http://127.0.0.1:{worker['port']}/p/"), again["Location"])
        with urllib.request.urlopen(again["Location"], timeout=20) as page2:
            self.assertEqual(page2.status, 200)
            self.assertIn("Typed New", page2.read().decode())

    # --- Load through a symlink alias lands on the same canonical worker ---
    def test_load_via_alias_reuses_worker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wp0104-alias-") as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            created = create_project_store(outside / "real", "Alias Target")
            alias = Path(tmp) / "aliasdir"
            try:
                alias.symlink_to(outside / "real", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable ({exc})")
            first = self._track(ws.start_worker(outside / "real"), str(created.location))
            second = ws.start_worker(alias)
            self.assertTrue(second["reused"])
            self.assertEqual(second["port"], first["port"])
            with urllib.request.urlopen(
                    f"{second['base']}{second['prefix']}/project/{second['project_id']}/papers/",
                    timeout=20) as page:
                self.assertEqual(page.status, 200)
                self.assertIn("Alias Target", page.read().decode())

    # --- Typed-path errors show the resolved target and change nothing ---
    def test_typed_errors_show_target_without_mutation(self) -> None:
        client = self._as_manager()
        ws.set_workspace_root(str(self.root))
        client.get("/")
        token = client.cookies["csrftoken"].value
        populated = self.root / "projA"
        before = sorted(p.name for p in populated.iterdir())
        response = client.post("/workspace/new/",
                               {"directory": str(populated), "name": "Clash"},
                               HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "refusing to overwrite")
        self.assertContains(response, "Resolved")
        self.assertEqual(sorted(p.name for p in populated.iterdir()), before)
        ghost = self.root / "ghost-missing"
        missing = client.post("/workspace/open/", {"directory": str(ghost)},
                              HTTP_X_CSRFTOKEN=token)
        self.assertEqual(missing.status_code, 200)
        self.assertContains(missing, "not a project directory")
        self.assertFalse(ghost.exists())

    # --- Manifest UUID for redirect assertions (read-only, stdlib only) ---
    @staticmethod
    def _uuid_of(directory: Path):
        from uuid import UUID

        doc = json.loads((directory / "project.json").read_text(encoding="utf-8"))
        return UUID(doc["project_uuid"])

    # --- Row counter tolerant of pre-migration stores ---
    @staticmethod
    def _table_count(db: Path, table: str) -> int:
        with sqlite3.connect(db) as connection:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if table not in names:
                return 0
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
