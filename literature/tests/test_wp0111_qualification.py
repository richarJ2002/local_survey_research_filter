"""
WP01.11 narrow regression qualification; synthetic stores only.

@File: test_wp0111_qualification.py
@Brief: Real revision mutations, lease continuity and concurrent drain/cancel.
@Date: 03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Custom library Imports
from django.db import transaction
from django.test import RequestFactory, SimpleTestCase, TestCase

from literature import workspace as ws
from literature.models import (
    Discovery,
    OpenCitationsLookup,
    Paper,
    PaperKeywordAssignment,
    Project,
    ScreeningDecision,
    ScreeningRevision,
    Search,
    SearchRun,
    Stage,
    SurveyCitation,
    SurveyEntry,
)
from literature.project_store import (
    ProjectStoreError,
    create_project_store,
    open_project_store,
    schema_status,
)
from literature.services.metrics_service import project_revision
from literature.tests.test_wp0105_lifecycle import _Client

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Actual before/after A mutations; B remains unchanged ---


class RevisionQualificationTests(TestCase):
    def test_all_relevant_content_edits_and_deletions_are_project_scoped(self):
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="B")
        search = Search.objects.create(project=a, name="Search A")
        paper = Paper.objects.create(title="A", doi="10.1000/a")
        Discovery.objects.create(search=search, paper=paper)
        other = Paper.objects.create(title="B", doi="10.1000/b")
        Discovery.objects.create(search=Search.objects.create(
            project=b, name="B"), paper=other)
        stage = Stage.objects.create(project=a, name="Title", order=1)
        decision = ScreeningDecision.objects.create(paper=paper, stage=stage)
        run = SearchRun.objects.create(search=search)
        assignment = PaperKeywordAssignment.objects.create(
            project=a, search=search, paper=paper, keyword="one"
        )
        first = SurveyEntry.objects.create(
            project=a, title="One", reference_number=7)
        second = SurveyEntry.objects.create(
            project=a, title="Two", reference_number=12)
        cache = OpenCitationsLookup.objects.create(
            doi=paper.doi, direction="references")
        foreign_cache = OpenCitationsLookup.objects.create(
            doi=other.doi, direction="references")
        before_b = project_revision(b)

        def changed(operation):
            before_a = project_revision(a)
            operation()
            self.assertNotEqual(project_revision(a), before_a)
            self.assertEqual(project_revision(b), before_b)

        # QuerySet.update deliberately bypasses auto_now: timestamp-only checks miss these.
        for row, field, value in (
            (a, "description", "Changed"),
            (search, "keywords", ["changed"]),
            (paper, "abstract", "Changed"),
            (stage, "name", "Changed"),
            (decision, "notes", "Changed"),
            (run, "results_count", 3),
            (assignment, "keyword", "changed"),
            (first, "title", "Changed"),
            (cache, "edges", [{"citing": paper.doi, "cited": "10.1000/x"}]),
        ):
            with self.subTest(model=type(row).__name__):
                changed(
                    lambda row=row, field=field, value=value: (
                        type(row).objects.filter(
                            pk=row.pk).update(**{field: value})
                    )
                )
        changed(lambda: SurveyCitation.objects.create(
            citing=first, cited=second))
        changed(lambda: SurveyCitation.objects.filter(citing=first).delete())
        changed(
            lambda: ScreeningRevision.objects.create(
                paper=paper,
                stage=stage,
                previous_decision="PENDING",
                new_decision="ACCEPT",
                reason="Synthetic correction",
            )
        )
        for row in (cache, second, assignment, run, decision, search):
            changed(row.delete)
        before_a = project_revision(a)
        foreign_cache.edges = [{"citing": other.doi, "cited": "10.1000/y"}]
        foreign_cache.save()
        self.assertEqual(project_revision(a), before_a)
        self.assertNotEqual(project_revision(b), before_b)

    def test_failed_atomic_edit_does_not_change_revision(self):
        project = Project.objects.create(name="Rollback")
        before = project_revision(project)
        with self.assertRaises(ValueError), transaction.atomic():
            Search.objects.create(project=project, name="Never committed")
            raise ValueError("synthetic rollback")
        self.assertEqual(project_revision(project), before)

    def test_cached_arxiv_and_curated_doi_alias_mutations_refresh_only_a(self):
        a = Project.objects.create(name="Alias A")
        b = Project.objects.create(name="Alias B")
        paper = Paper.objects.create(title="Arxiv", source_ids={
                                     "arxiv": "2401.00001v2"})
        Discovery.objects.create(
            paper=paper, search=Search.objects.create(project=a, name="A"))
        SurveyEntry.objects.create(
            project=a,
            title="Alias snapshot",
            reference_number=7,
            identity_aliases=["https://doi.org/10.1000/alias"],
        )
        before_b = project_revision(b)
        for doi in ("10.48550/arxiv.2401.00001", "10.1000/alias"):
            before = project_revision(a)
            cache = OpenCitationsLookup.objects.create(
                doi=doi, direction="outgoing", edges=[])
            self.assertNotEqual(project_revision(a), before)
            before = project_revision(a)
            cache.delete()
            self.assertNotEqual(project_revision(a), before)
            self.assertEqual(project_revision(b), before_b)


# --- Real owned worker HTTP, admission overlaps manager drain ---
class DrainQualificationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory(prefix="wp11-functional-")
        cls.root = Path(cls.tmp.name)
        cls.environment = patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(cls.root / "state"),
                "XDG_CACHE_HOME": str(cls.root / "cache"),
                "PYTHON_DOTENV_DISABLED": "1",
            },
        )
        cls.environment.start()
        cls.store = create_project_store(
            cls.root / "project", "Synthetic drain")

    @classmethod
    def tearDownClass(cls):
        canonical = str(cls.store.location)
        ws.sweep_idle(lease_ttl=0, worker_grace=0)
        child = ws._CHILDREN.get(canonical)
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=20)
        ws.forget_worker(canonical)
        cls.environment.stop()
        cls.tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.tab = ws.acquire_handle(self.store.location)
        self.canonical = str(self.store.location)
        self.base = f"{self.tab['base']}{self.tab['prefix']}/project/{self.tab['project_id']}"
        self.writer = _Client(self.tab["handle_id"])
        self.control = _Client()
        self.assertEqual(self.writer.get(self.base + "/search/new/")[0], 200)
        self.assertEqual(self.control.get(
            self.base + "/lifecycle/drain/")[0], 200)
        self.addCleanup(ws.release_handle, self.tab["handle_id"])

    def snapshot(self):
        status, body, _ = self.control.get(self.base + "/lifecycle/drain/")
        self.assertEqual(status, 200)
        return json.loads(body)

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("functional condition did not become true")

    def save(self, name):
        return self.writer.post(
            self.base + "/search/new/",
            {
                "name": name,
                "keywords_json": '["synthetic"]',
                "venues_json": "[]",
                "author_names_json": "[]",
            },
        )

    def test_manager_waits_while_admitted_write_commits(self):
        db = self.store.location / "project.sqlite3"
        with sqlite3.connect(db, isolation_level=None) as lock, ThreadPoolExecutor(2) as pool:
            lock.execute("BEGIN EXCLUSIVE")
            try:
                write = pool.submit(self.save, "Committed during manager wait")
                self.wait(lambda: self.snapshot()["in_flight"] == 1)
                ws.release_handle(self.tab["handle_id"])
                close = pool.submit(ws.drain_and_stop, self.canonical, 15)
                self.wait(lambda: self.snapshot()["draining"])
                time.sleep(0.6)  # Forces at least one real GET polling round.
                self.assertFalse(close.done())
                self.assertFalse(write.done())
            finally:
                lock.rollback()
            self.assertEqual(write.result(timeout=20)[0], 200)
            self.assertTrue(close.result(timeout=20)["stopped"])
        with sqlite3.connect(db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM literature_search WHERE name=?",
                    ("Committed during manager wait",),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute(
                "PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute(
                "PRAGMA foreign_key_check").fetchall(), [])

    def test_refused_closure_and_concurrent_attach_leave_survivor_writable(self):
        ws.release_handle(self.tab["handle_id"])
        with (
            patch.object(
                ws, "stop_worker", return_value={"stopped": False, "reason": "synthetic-refusal"}
            ),
            ThreadPoolExecutor(2) as pool,
        ):
            closed = pool.submit(ws.drain_and_stop, self.canonical)
            opened = pool.submit(ws.acquire_handle, self.store.location)
            self.assertFalse(closed.result(timeout=20)["stopped"])
            survivor = opened.result(timeout=20)
        self.addCleanup(ws.release_handle, survivor["handle_id"])
        self.writer.handle = survivor["handle_id"]
        self.assertFalse(self.snapshot()["draining"])
        self.assertEqual(self.save("Survivor after refused close")[0], 200)

    def test_live_handle_refusal_never_sets_drain(self):
        result = ws.drain_and_stop(self.canonical)
        self.assertEqual(result["reason"], "handles-live")
        self.assertFalse(self.snapshot()["draining"])
        self.assertEqual(
            self.save("Survivor after live handle refusal")[0], 200)

    def test_timeout_cancels_drain_and_survivor_can_save(self):
        with (
            sqlite3.connect(self.store.location / "project.sqlite3", isolation_level=None) as lock,
            ThreadPoolExecutor(2) as pool,
        ):
            lock.execute("BEGIN EXCLUSIVE")
            try:
                write = pool.submit(self.save, "Committed after timeout")
                self.wait(lambda: self.snapshot()["in_flight"] == 1)
                ws.release_handle(self.tab["handle_id"])
                close = pool.submit(ws.drain_and_stop, self.canonical, 0.2)
                self.assertEqual(close.result(timeout=10)[
                                 "reason"], "drain-timeout")
                self.assertFalse(self.snapshot()["draining"])
            finally:
                lock.rollback()
            self.assertEqual(write.result(timeout=20)[0], 200)
        survivor = ws.acquire_handle(self.store.location)
        self.addCleanup(ws.release_handle, survivor["handle_id"])
        self.writer.handle = survivor["handle_id"]
        self.assertEqual(self.save("Survivor after canceled timeout")[0], 200)

    def test_same_tab_navigation_and_editor_keep_single_lease(self):
        lease = ws._lease_row(self.tab["handle_id"])
        for _ in range(4):
            result = ws.attach_lease(
                self.canonical,
                self.tab["project_uuid"],
                self.tab["epoch"],
                self.tab["port"],
                lease["handle_id"],
                lease["tab_token"],
                "same_tab",
            )
            self.assertTrue(result["reused"])
            self.assertEqual(result["handle_id"], lease["handle_id"])
        self.assertEqual(ws.live_handle_count(self.canonical), 1)
        ws.release_handle(lease["handle_id"])
        self.assertEqual(ws.live_handle_count(self.canonical), 0)

    def test_release_delete_failure_retains_lease_and_absence_is_idempotent(self):
        from literature.workspace_views import project_lifecycle

        connect = sqlite3.connect

        class FailedDelete(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("DELETE FROM leases"):
                    raise sqlite3.OperationalError(
                        "synthetic registry DELETE failure")
                return super().execute(sql, *args, **kwargs)

        request = RequestFactory().post(
            self.base +
            "/lifecycle/release/", {"handle_id": self.tab["handle_id"]}
        )
        bound = {"ok": True, "store": self.canonical,
                 "uuid": self.tab["project_uuid"]}
        # Exercise the actual decorated lifecycle view and registry DELETE; only
        # its fixed-store/Project lookup is supplied without a second ORM binding.
        with (
            patch("literature.worker.bound_store", return_value=bound),
            patch(
                "literature.workspace_views.get_object_or_404",
                return_value=SimpleNamespace(pk=self.tab["project_id"]),
            ),
        ):
            with patch.object(
                ws.sqlite3,
                "connect",
                side_effect=lambda *a, **kw: connect(
                    *a, **kw, factory=FailedDelete),
            ):
                with self.assertRaisesRegex(ProjectStoreError, "cannot release"):
                    ws.release_handle(self.tab["handle_id"])
                response = project_lifecycle(
                    request, self.tab["project_id"], "release")
                self.assertEqual(response.status_code, 503)
                self.assertFalse(json.loads(response.content)["released"])
            self.assertIsNotNone(ws.heartbeat_handle(self.tab["handle_id"]))
            self.assertEqual(ws.live_handle_count(self.canonical), 1)
            # First release, then already-absent idempotent retry.
            for _ in range(2):
                response = project_lifecycle(
                    request, self.tab["project_id"], "release")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(json.loads(response.content)["released"])
                self.assertIsNone(ws._lease_row(self.tab["handle_id"]))
            self.assertTrue(ws.release_handle(self.tab["handle_id"]))

    def test_no_migration_drift_against_two_opened_synthetic_stores(self):
        """
        Check opened synthetic schemas using the guarded developer entry.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        other = create_project_store(self.root / "drift B", "Synthetic B")
        tab = ws.acquire_handle(other.location)
        self.addCleanup(ws.release_handle, tab["handle_id"])
        self.addCleanup(lambda: ws.sweep_idle(lease_ttl=0, worker_grace=0))
        for store in (self.store, other):
            opened = open_project_store(store.location)
            db = opened.location / "project.sqlite3"
            self.assertTrue(schema_status(db).in_sync)
            result = subprocess.run(
                [sys.executable, "launch.py", "django", "makemigrations",
                 "--check", "--dry-run"],
                env=dict(os.environ, LOCAL_SURVEY_DB=str(db),
                         WP_ROLE="developer"),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No changes detected", result.stdout)
