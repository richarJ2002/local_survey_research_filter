"""
WP01.08 synthetic copies, closure, atomic recovery and real HTTP manager UX.

@File: test_wp0108_copy_recovery.py
@Brief: Committed WAL snapshots, package privacy, roundtrip and recovery gates.
@Date: 03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import errno
import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

# Custom library Imports
from django.conf import settings
from django.core.wsgi import get_wsgi_application
from django.test import Client, SimpleTestCase, TestCase, override_settings

from literature import project_store as ps
from literature import workspace as ws
from literature.models import (
    Discovery,
    Paper,
    PaperKeywordAssignment,
    Project,
    ScreeningDecision,
    Search,
)
from literature.services.export_service import build_markdown
from literature.tests.test_wp0105_lifecycle import _Client
from literature.worker import manager_control_base

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

# --- Realistic records built in a child with a fixed synthetic default DB ---
_SEED = """
import django
django.setup()
from literature.models import *
from django.utils import timezone
p = Project.objects.get()
s = Search.objects.create(project=p, name='Audit search', keywords=['alpha', 'beta'])
r = SearchRun.objects.create(search=s, source='manual', status='completed',
    results_count=12, duplicates_count=4, no_abstract_count=3, incomplete_count=2,
    query_used='historical query')
papers = []
for i in range(3):
    paper = Paper.objects.create(title=f'Paper {i}', doi=f'10.1000/roundtrip-{i}',
        authors=[{'name': 'Researcher'}], abstract='Content', publication_year=2020+i,
        source_ids={'arxiv': f'2001.0000{i}'}, sources=['arxiv'], raw={'cached': i},
        references=[f'10.1000/roundtrip-{(i+1)%3}'], reference_text='Bibliography')
    papers.append(paper)
    Discovery.objects.create(search=s, search_run=r, paper=paper, source='manual',
        query='historical query', raw_payload={'audit': i})
    PaperKeywordAssignment.objects.create(project=p, search=s, paper=paper, keyword='alpha')
    for stage in p.stages.all():
        decision = ['ACCEPT', 'REJECT', 'PENDING'][i]
        ScreeningDecision.objects.create(stage=stage, paper=paper, decision=decision,
            reason='Reason', notes=f'Notes {i}', decided_at=timezone.now() if i < 2 else None)
ScreeningRevision.objects.create(paper=papers[0], stage=p.stages.first(),
    previous_decision='REJECT', new_decision='ACCEPT', reason='Explicit correction')
OpenCitationsLookup.objects.create(doi=papers[0].doi, direction='outgoing',
    edges=[{'citing': papers[0].doi, 'cited': papers[1].doi}])
entries=[]
for i, paper in enumerate(papers):
    entries.append(SurveyEntry.objects.create(project=p, paper=paper,
        reference_number=7+i*5, title=f'Curated {i}', authors='Researcher',
        doi=paper.doi, abstract='Curated abstract', reference_text='Curated refs',
        reference_ids=paper.references, identity_aliases=[f'arxiv:2001.0000{i}'],
        origin_source='arxiv', inclusion_origin='reading'))
for i in range(3):
    SurveyCitation.objects.create(citing=entries[i], cited=entries[(i+1)%3])
"""


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes
class CopyRecoveryTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture_tmp = tempfile.TemporaryDirectory(prefix="wp08-fixture-")
        cls.fixture = ps.create_project_store(
            Path(cls.fixture_tmp.name) / "fixture", "Roundtrip")
        result = subprocess.run(
            [sys.executable, "-c", _SEED],
            cwd=settings.BASE_DIR,
            env=dict(
                os.environ,
                DJANGO_SETTINGS_MODULE="config.settings",
                LOCAL_SURVEY_DB=str(cls.fixture.location /
                                    ps.DATABASE_FILENAME),
            ),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    @classmethod
    def tearDownClass(cls):
        cls.fixture_tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wp08-case-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(self.root / "private"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.source = ps.save_copy_store(
            self.fixture.location, self.root / "source")
        self.db = self.source.location / ps.DATABASE_FILENAME
        self.addCleanup(self._stop_owned)

    def _stop_owned(self):
        for canonical, child in list(ws._CHILDREN.items()):
            if Path(canonical).is_relative_to(self.root):
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=15)
                ws._CHILDREN.pop(canonical, None)

    @staticmethod
    def rows(directory):
        with closing(ps._connect_ro(Path(directory) / ps.DATABASE_FILENAME)) as connection:
            return {
                name: tuple(connection.execute(
                    f'SELECT * FROM "{name}" ORDER BY id'))
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'literature_%' ORDER BY name"
                )
            }

    def test_copy_backup_restore_roundtrip_exact_research_rows_and_recovery(self):
        before = self.rows(self.source.location)
        identity = (self.source.location / ps.MANIFEST_FILENAME).read_bytes()
        copied = ps.save_copy_store(self.source.location, self.root / "copy")
        self.assertNotEqual(copied.project_uuid, self.source.project_uuid)
        self.assertEqual(self.rows(copied.location), before)
        backup = ws.operate_store(
            "backup", self.source.location, self.root / "backup")
        self.assertEqual(backup.project_uuid, self.source.project_uuid)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE literature_paper SET abstract='changed'")
        changed = self.rows(self.source.location)
        restored = ws.operate_store(
            "restore", backup.location, self.source.location)
        self.assertEqual(restored.project_uuid, self.source.project_uuid)
        self.assertEqual(self.rows(restored.location), before)
        recovery = list(self.root.glob(".source.recovery-*"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual(restored.recovery_location, recovery[0])
        self.assertEqual(self.rows(recovery[0]), changed)
        self.assertEqual(
            (self.source.location / ps.MANIFEST_FILENAME).read_bytes(), identity)
        ps.validate_package(copied.location)

    def test_committed_wal_during_real_writer_and_pinned_counts(self):
        writer = sqlite3.connect(self.db, check_same_thread=False)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE literature_paper SET abstract='committed WAL'")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE literature_paper SET abstract='uncommitted'")
        before = self.db.read_bytes()
        wal = Path(str(self.db) + "-wal").read_bytes()
        copied = ps.save_copy_store(
            self.source.location, self.root / "wal-copy")
        with closing(ps._connect_ro(copied.location / ps.DATABASE_FILENAME)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT DISTINCT abstract FROM literature_paper").fetchall(),
                [("committed WAL",)],
            )
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(Path(str(self.db) + "-wal").read_bytes(), wal)
        writer.rollback()

        original_counts = ps._table_counts
        ready = threading.Event()
        committed = threading.Event()

        def write_after_snapshot():
            self.assertTrue(ready.wait(10))
            writer.execute("DELETE FROM literature_opencitationslookup")
            writer.commit()
            committed.set()

        def counts(connection):
            result = original_counts(connection)
            if connection.in_transaction and not ready.is_set():
                ready.set()
                self.assertTrue(committed.wait(10))
            return result

        thread = threading.Thread(target=write_after_snapshot)
        thread.start()
        with patch.object(ps, "_table_counts", side_effect=counts):
            pinned = ps.save_copy_store(
                self.source.location, self.root / "pinned")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.rows(pinned.location)[
                         "literature_opencitationslookup"]), 1)
        self.assertEqual(len(self.rows(self.source.location)[
                         "literature_opencitationslookup"]), 0)

    def test_required_external_symlinks_materialised_optional_files_not_read(self):
        external = self.root / "external"
        external.mkdir()
        for name in ps.PACKAGE_MEMBERS:
            member = self.source.location / name
            member.rename(external / name)
            member.symlink_to(external / name)
        (self.source.location / "exports").symlink_to(
            self.root / "private", target_is_directory=True
        )
        (self.source.location / ".env").symlink_to(self.root / "DO-NOT-READ")
        (self.source.location / "backups").mkdir()
        (self.source.location / "registry.sqlite3").write_text("private sentinel")
        alias = self.root / "alias"
        alias.symlink_to(self.source.location, target_is_directory=True)
        copy = ps.save_copy_store(alias, self.root / "materialised")
        self.assertEqual(
            {p.name for p in copy.location.iterdir()}, ps.PACKAGE_MEMBERS)
        self.assertTrue(all(not p.is_symlink()
                        for p in copy.location.iterdir()))
        for name in ps.PACKAGE_MEMBERS:
            (external / name).rename(external / (name + ".unavailable"))
        ps.validate_package(copy.location)
        self.assertEqual(self.rows(copy.location),
                         self.rows(self.fixture.location))

    def test_private_auth_session_rows_and_deleted_bytes_excluded(self):
        sentinel = "PRIVATE-SESSION-CREDENTIAL-SENTINEL-86b44"
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO django_session VALUES ('synthetic', ?, '2030-01-01')", (sentinel,)
            )
            connection.execute(
                "INSERT INTO auth_user (password,last_login,is_superuser,username,last_name,email,is_staff,is_active,date_joined,first_name) VALUES (?,NULL,0,'synthetic','','',0,1,'2026-01-01','')",
                (sentinel,),
            )
        copied = ps.save_copy_store(
            self.source.location, self.root / "private-free")
        self.assertNotIn(sentinel.encode(), (copied.location /
                         ps.DATABASE_FILENAME).read_bytes())
        with closing(ps._connect_ro(copied.location / ps.DATABASE_FILENAME)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_session").fetchone(), (0,)
            )
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM auth_user").fetchone(), (0,))
        with closing(ps._connect_ro(self.db)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM auth_user").fetchone(), (1,))

    def test_unknown_schema_objects_refused_without_output(self):
        for ddl in (
            "CREATE TABLE private_registry(secret TEXT)",
            "CREATE VIEW bad_view AS SELECT * FROM literature_project",
            "CREATE TRIGGER bad_trigger AFTER UPDATE ON literature_project BEGIN DELETE FROM literature_paper; END",
            "ALTER TABLE literature_project ADD COLUMN credential TEXT",
            "CREATE INDEX surprise ON literature_project(name)",
        ):
            with self.subTest(ddl=ddl):
                store = ps.save_copy_store(
                    self.fixture.location,
                    self.root / ("bad-" + str(len(list(self.root.iterdir())))),
                )
                with sqlite3.connect(store.location / ps.DATABASE_FILENAME) as connection:
                    connection.execute(ddl)
                destination = self.root / "refused"
                with self.assertRaises(ps.ProjectStoreError):
                    ps.save_copy_store(store.location, destination)
                self.assertFalse(destination.exists())

    def test_sqlite_lookalike_table_and_executable_trigger_refused(self):
        for kind in ("table", "trigger"):
            with self.subTest(kind=kind):
                source = ps.save_copy_store(
                    self.fixture.location, self.root / kind)
                db = source.location / ps.DATABASE_FILENAME
                sentinel = "synthetic-auth-secret-wp08-p1"
                with sqlite3.connect(db) as connection:
                    if kind == "table":
                        connection.execute(
                            "CREATE TABLE sqliteXprivate(secret TEXT)")
                        connection.execute(
                            "INSERT INTO sqliteXprivate VALUES (?)", (sentinel,))
                    else:
                        connection.execute(
                            "INSERT INTO auth_user (password,last_login,is_superuser,username,last_name,email,is_staff,is_active,date_joined,first_name) VALUES (?,NULL,0,'synthetic','','',0,1,'2026-01-01','')",
                            (sentinel,),
                        )
                        connection.execute(
                            "CREATE TRIGGER sqliteXleak BEFORE DELETE ON auth_user "
                            "BEGIN UPDATE literature_project SET description=OLD.password; END"
                        )
                before = db.read_bytes()
                with closing(ps._connect_ro(db)) as connection:
                    names = {row[1] for row in ps._schema(connection)}
                    self.assertIn("sqliteXprivate" if kind ==
                                  "table" else "sqliteXleak", names)
                    if kind == "table":
                        self.assertEqual(ps._table_counts(
                            connection)["sqliteXprivate"], 1)
                destination = self.root / (kind + "-output")
                with self.assertRaises(ps.ProjectStoreError):
                    ps.save_copy_store(source.location, destination)
                self.assertFalse(destination.exists())
                self.assertEqual(db.read_bytes(), before)
                with closing(ps._connect_ro(db)) as connection:
                    self.assertNotEqual(
                        connection.execute(
                            "SELECT description FROM literature_project").fetchone(),
                        (sentinel,),
                    )

    def test_copy_and_backup_refuse_locked_source_retarget_or_replacement(self):
        foreign = ps.save_copy_store(
            self.fixture.location, self.root / "foreign")
        foreign_db = foreign.location / ps.DATABASE_FILENAME
        with sqlite3.connect(foreign_db) as connection:
            connection.execute(
                "UPDATE literature_project SET description='foreign metadata'")
        foreign_before = foreign_db.read_bytes()
        original_backup = ps._backup_db_file
        for action in (ps.save_copy_store, ps.backup_project_store):
            for mode in ("symlink", "replacement"):
                with self.subTest(action=action.__name__, mode=mode):
                    source = ps.save_copy_store(
                        self.fixture.location, self.root /
                        (action.__name__ + mode)
                    )
                    member = source.location / ps.DATABASE_FILENAME
                    parked = self.root / \
                        (action.__name__ + mode + "-original.sqlite3")
                    identity_before = (source.location /
                                       ps.MANIFEST_FILENAME).read_bytes()
                    if mode == "symlink":
                        member.rename(parked)
                        member.symlink_to(parked)
                    source_before = (parked if mode ==
                                     "symlink" else member).read_bytes()

                    def retarget(
                        *args, mode=mode, member=member, parked=parked, source=source, **kwargs
                    ):
                        if mode == "symlink":
                            member.unlink()
                            member.symlink_to(foreign_db)
                        else:
                            member.rename(parked)
                            # A hard-link replacement has a different inode,
                            # without using a raw live-database copy.
                            os.link(foreign_db, member)
                        # The logical path now names B, but the snapshot must
                        # still be the retained connection to locked A.
                        self.assertIsInstance(args[0], sqlite3.Connection)
                        self.assertEqual(
                            args[0]
                            .execute("SELECT project_uuid FROM wp_store_identity")
                            .fetchone(),
                            (str(source.project_uuid),),
                        )
                        self.assertNotEqual(
                            args[0]
                            .execute("SELECT description FROM literature_project")
                            .fetchone(),
                            ("foreign metadata",),
                        )
                        return original_backup(*args, **kwargs)

                    destination = self.root / \
                        (action.__name__ + mode + "-output")
                    with (
                        ps.store_file_lock(foreign, closed=False),
                        patch.object(ps, "_backup_db_file",
                                     side_effect=retarget),
                        self.assertRaises(ps.ProjectStoreError),
                    ):
                        action(source.location, destination)
                    self.assertFalse(destination.exists())
                    self.assertEqual(parked.read_bytes(), source_before)
                    self.assertEqual(foreign_db.read_bytes(), foreign_before)
                    self.assertEqual(
                        (source.location /
                         ps.MANIFEST_FILENAME).read_bytes(), identity_before
                    )
                    for recovery in self.root.glob(f".{destination.name}.recovery-*"):
                        staged_db = recovery / ps.DATABASE_FILENAME
                        if staged_db.exists():
                            self.assertNotIn(
                                b"foreign metadata", staged_db.read_bytes())

    def test_same_uuid_foreign_inode_replacement_still_refused(self):
        foreign = ps.backup_project_store(
            self.source.location, self.root / "same-uuid")
        foreign_db = foreign.location / ps.DATABASE_FILENAME
        with sqlite3.connect(foreign_db) as connection:
            connection.execute(
                "UPDATE literature_project SET description='same-UUID foreign metadata'"
            )
        original_backup = ps._backup_db_file
        parked = self.root / "original.sqlite3"
        before = self.db.read_bytes()

        def replace_member(*args, **kwargs):
            self.db.rename(parked)
            os.link(foreign_db, self.db)
            return original_backup(*args, **kwargs)

        destination = self.root / "same-uuid-refused"
        with (
            ps.store_file_lock(foreign, closed=False),
            patch.object(ps, "_backup_db_file", side_effect=replace_member),
            self.assertRaises(ps.ProjectStoreError),
        ):
            ps.backup_project_store(self.source.location, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(parked.read_bytes(), before)

    def test_foreign_snapshot_original_uuid_checked_before_sanitising_or_relabel(self):
        foreign = ps.save_copy_store(
            self.fixture.location, self.root / "foreign")
        foreign_db = foreign.location / ps.DATABASE_FILENAME
        with sqlite3.connect(foreign_db) as connection:
            connection.execute(
                "INSERT INTO django_session VALUES ('synthetic', 'synthetic-session', '2030-01-01')"
            )
        original_backup = ps._backup_db_file
        source_before = self.db.read_bytes()

        def substitute_snapshot(source, destination, **kwargs):
            return original_backup(foreign_db, destination)

        destination = self.root / "not-relabelled"
        with (
            patch.object(ps, "_backup_db_file",
                         side_effect=substitute_snapshot),
            self.assertRaises(ps.ProjectStoreError),
        ):
            ps.save_copy_store(self.source.location, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.db.read_bytes(), source_before)
        recovery = next(self.root.glob(".not-relabelled.recovery-*"))
        with closing(ps._connect_ro(recovery / ps.DATABASE_FILENAME)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT project_uuid FROM wp_store_identity").fetchone(),
                (str(foreign.project_uuid),),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM django_session").fetchone(), (1,)
            )

    def test_restore_refuses_backup_retarget_and_target_replacement(self):
        backup = ps.backup_project_store(
            self.source.location, self.root / "backup")
        foreign = ps.save_copy_store(
            self.fixture.location, self.root / "foreign")
        foreign_db = foreign.location / ps.DATABASE_FILENAME
        original_backup = ps._backup_db_file
        original_stage = ps._stage_package
        before = self.rows(self.source.location)
        parked_backup = self.root / "parked-backup.sqlite3"

        def retarget(*args, **kwargs):
            member = backup.location / ps.DATABASE_FILENAME
            member.rename(parked_backup)
            member.symlink_to(foreign_db)
            return original_backup(*args, **kwargs)

        with (
            patch.object(ps, "_backup_db_file", side_effect=retarget),
            self.assertRaises(ps.ProjectStoreError),
        ):
            ps.restore_project_store(backup.location, self.source.location)
        self.assertEqual(self.rows(self.source.location), before)
        (backup.location / ps.DATABASE_FILENAME).unlink()
        parked_backup.rename(backup.location / ps.DATABASE_FILENAME)
        parked_target = self.root / "parked-target.sqlite3"

        def replace_target(*args, **kwargs):
            result = original_stage(*args, **kwargs)
            self.db.rename(parked_target)
            os.link(foreign_db, self.db)
            return result

        manifest_before = (self.source.location /
                           ps.MANIFEST_FILENAME).read_bytes()
        with (
            patch.object(ps, "_stage_package", side_effect=replace_target),
            self.assertRaises(ps.ProjectStoreError),
        ):
            ps.restore_project_store(backup.location, self.source.location)
        self.assertEqual(
            (self.source.location / ps.MANIFEST_FILENAME).read_bytes(), manifest_before
        )
        with closing(ps._connect_ro(parked_target)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT project_uuid FROM wp_store_identity").fetchone(),
                (str(self.source.project_uuid),),
            )

    def test_restore_exchange_racing_target_change_is_rolled_back(self):
        backup = ps.backup_project_store(
            self.source.location, self.root / "backup")
        foreign = ps.save_copy_store(
            self.fixture.location, self.root / "foreign")
        foreign_db = foreign.location / ps.DATABASE_FILENAME
        with sqlite3.connect(foreign_db) as connection:
            connection.execute(
                "UPDATE literature_project SET description='foreign metadata'")
        foreign_before = foreign_db.read_bytes()
        parked = self.root / "original-target.sqlite3"
        original_before = self.db.read_bytes()
        manifest_before = (self.source.location /
                           ps.MANIFEST_FILENAME).read_bytes()
        original_rename = ps._rename
        exchanges = []

        def race_exchange(source, target, *, exchange=False):
            if not exchanges:
                self.db.rename(parked)
                os.link(foreign_db, self.db)
            exchanges.append(exchange)
            return original_rename(source, target, exchange=exchange)

        with (
            ps.store_file_lock(foreign, closed=False),
            patch.object(ps, "_rename", side_effect=race_exchange),
            self.assertRaises(ps.ProjectStoreError),
        ):
            ps.restore_project_store(backup.location, self.source.location)
        self.assertEqual(exchanges, [True, True])
        self.assertEqual(parked.read_bytes(), original_before)
        self.assertEqual(foreign_db.read_bytes(), foreign_before)
        self.assertEqual(self.db.stat().st_ino, foreign_db.stat().st_ino)
        self.assertEqual(
            (self.source.location / ps.MANIFEST_FILENAME).read_bytes(), manifest_before
        )

    def test_unknown_package_members_manifest_and_uuid_refused(self):
        backup = ps.backup_project_store(
            self.source.location, self.root / "backup")
        unknown = backup.location / "untrusted.py"
        unknown.write_text("raise AssertionError('never execute')")
        with self.assertRaises(ps.ProjectStoreError):
            ps.restore_project_store(backup.location, self.source.location)
        unknown.unlink()
        manifest_path = backup.location / ps.MANIFEST_FILENAME
        doc = json.loads(manifest_path.read_text())
        for change in (
            {"secret": "ignored?"},
            {"database": "../elsewhere"},
            {"format_version": 999},
            {"project_uuid": str(self.fixture.project_uuid)},
        ):
            with self.subTest(change=change):
                manifest_path.write_text(json.dumps(doc | change))
                with self.assertRaises(ps.ProjectStoreError):
                    ps.restore_project_store(
                        backup.location, self.source.location)
        manifest_path.write_text(json.dumps(doc))
        with self.assertRaises(ps.ProjectStoreError):
            ps.restore_project_store(
                self.fixture.location, self.source.location)

    def test_populated_empty_symlink_and_racing_destinations(self):
        populated = self.root / "populated"
        populated.mkdir()
        sentinel = populated / "keep"
        sentinel.write_text("keep")
        with self.assertRaises(ps.ProjectStoreError):
            ps.save_copy_store(self.source.location, populated)
        self.assertEqual(sentinel.read_text(), "keep")
        alias = self.root / "output-link"
        alias.symlink_to(populated, target_is_directory=True)
        with self.assertRaises(ps.ProjectStoreError):
            ps.save_copy_store(self.source.location, alias)
        empty = self.root / "empty"
        empty.mkdir()
        ps.save_copy_store(self.source.location, empty)
        race = self.root / "race"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(ps.save_copy_store, self.source.location, race) for _ in range(2)
            ]
        self.assertEqual(sum(f.exception() is None for f in futures), 1)
        ps.validate_package(race)

    def test_failures_disk_full_permission_interruption_keep_source_and_stage(self):
        before = self.rows(self.source.location)
        for index, failure in enumerate(
            (OSError(errno.ENOSPC, "full"), PermissionError(
                "denied"), KeyboardInterrupt())
        ):
            with self.subTest(failure=type(failure).__name__):
                target = self.root / f"fail-{index}"
                with (
                    patch.object(ps, "_backup_db_file", side_effect=failure),
                    self.assertRaises(
                        KeyboardInterrupt
                        if isinstance(failure, KeyboardInterrupt)
                        else ps.ProjectStoreError
                    ),
                ):
                    ps.save_copy_store(self.source.location, target)
                self.assertFalse(target.exists())
                self.assertTrue(
                    list(self.root.glob(f".{target.name}.recovery-*")))
                self.assertEqual(self.rows(self.source.location), before)

    def test_restore_atomic_exchange_failure_and_postexchange_interruption(self):
        backup = ps.backup_project_store(
            self.source.location, self.root / "backup")
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE literature_paper SET abstract='prior'")
        before = self.rows(self.source.location)
        with (
            patch.object(ps, "_rename", side_effect=OSError("rename denied")),
            self.assertRaises(ps.ProjectStoreError),
        ):
            ps.restore_project_store(backup.location, self.source.location)
        self.assertEqual(self.rows(self.source.location), before)
        real_rename = ps._rename

        def exchange_then_interrupt(*args, **kwargs):
            real_rename(*args, **kwargs)
            raise KeyboardInterrupt

        with (
            patch.object(ps, "_rename", side_effect=exchange_then_interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            ps.restore_project_store(backup.location, self.source.location)
        self.assertEqual(self.rows(self.source.location),
                         self.rows(backup.location))
        recoveries = [self.rows(p)
                      for p in self.root.glob(".source.recovery-*")]
        self.assertIn(before, recoveries)

    def test_live_worker_even_missing_registry_or_foreign_xdg_is_not_closed(self):
        worker = ws.acquire_handle(self.source.location)
        for mode in ("normal", "missing-registry", "foreign-xdg"):
            with self.subTest(mode=mode):
                if mode == "missing-registry":
                    ws.forget_worker(str(self.source.location))
                    ws.release_handle(worker["handle_id"])
                context = (
                    patch.dict(
                        os.environ, {"XDG_DATA_HOME": str(self.root / "foreign")})
                    if mode == "foreign-xdg"
                    else patch.dict(os.environ, {})
                )
                with context:
                    with self.assertRaises(ps.ProjectStoreError):
                        ps.backup_project_store(
                            self.source.location, self.root / "no-backup")
                    with self.assertRaises(ps.ProjectStoreError):
                        ws.operate_store(
                            "backup", self.source.location, self.root / "no-backup")
                self.assertFalse((self.root / "no-backup").exists())
        copied = ws.operate_store(
            "copy", self.source.location, self.root / "open-copy")
        self.assertNotEqual(copied.project_uuid, self.source.project_uuid)

    def test_active_restore_target_refused_and_same_uuid_load_collision(self):
        backup = ps.backup_project_store(
            self.source.location, self.root / "backup")
        worker = ws.acquire_handle(self.source.location)
        with self.assertRaises(ps.ProjectStoreError):
            ps.restore_project_store(backup.location, self.source.location)
        with self.assertRaisesRegex(ps.ProjectStoreError, "same-UUID collision"):
            ws.start_worker(backup.location)
        self.assertEqual(ws.find_worker(str(self.source.location))[
                         "pid"], worker["pid"])

    def test_operation_blocks_acquire_until_publication(self):
        ready, release = threading.Event(), threading.Event()
        real_stage = ps._stage_package

        def stage(*args):
            ready.set()
            self.assertTrue(release.wait(15))
            return real_stage(*args)

        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(ps, "_stage_package", side_effect=stage),
        ):
            copy = pool.submit(
                ws.operate_store, "backup", self.source.location, self.root / "backup"
            )
            self.assertTrue(ready.wait(10))
            acquire = pool.submit(ws.acquire_handle, self.source.location)
            self.assertFalse(acquire.done())
            release.set()
            backup = copy.result(timeout=30)
            worker = acquire.result(timeout=30)
        self.assertEqual(backup.project_uuid, self.source.project_uuid)
        self.assertEqual(worker["project_uuid"], str(self.source.project_uuid))

    def test_clean_workspace_copy_load_over_real_worker_http(self):
        copied = ps.save_copy_store(
            self.source.location, self.root / "portable")
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "clean-user")}):
            worker = ws.acquire_handle(copied.location)
            client = _Client(worker["handle_id"])
            base = f"{worker['base']}{worker['prefix']}/project/{worker['project_id']}"
            for page in ("papers/", "survey/", "export/markdown/"):
                status, body, _ = client.get(f"{base}/{page}")
                self.assertEqual(status, 200)
                self.assertIn(b"Roundtrip", body)
            self.assertEqual(self.rows(copied.location),
                             self.rows(self.source.location))

    def test_external_linked_db_worker_handles_and_copy(self):
        external = self.root / "required-data.sqlite3"
        self.db.rename(external)
        self.db.symlink_to(external)
        worker = ws.acquire_handle(self.source.location)
        client = _Client(worker["handle_id"])
        base = f"{worker['base']}{worker['prefix']}/project/{worker['project_id']}"
        status, _, _ = client.get(base + "/papers/")
        self.assertEqual(status, 200)
        # a genuine form page primes the worker CSRF cookie
        client.get(base + "/")
        status, _, _ = client.post(
            base + "/lifecycle/heartbeat/", {"handle_id": worker["handle_id"]}
        )
        self.assertEqual(status, 200)
        ps.save_copy_store(self.source.location, self.root / "copy")
        with self.assertRaises(ps.ProjectStoreError):
            ps.backup_project_store(
                self.source.location, self.root / "blocked")

    def test_corrupt_schema_history_and_fk_refused(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE literature_discovery SET paper_id=999999 WHERE id=1")
        with self.assertRaises(ps.ProjectStoreError):
            ps.save_copy_store(self.source.location, self.root / "bad-fk")
        self.assertFalse((self.root / "bad-fk").exists())

    def test_unowned_papers_and_unknown_load_members_refused(self):
        extra = self.source.location / "unrelated.txt"
        extra.write_text("not part of a recognised store")
        with self.assertRaisesRegex(ps.ProjectStoreError, "unknown project members"):
            ws.check_load_target(str(self.source.location))
        extra.unlink()
        with sqlite3.connect(self.db) as connection:
            connection.execute("DELETE FROM literature_surveycitation")
            connection.execute("DELETE FROM literature_surveyentry")
            connection.execute("DELETE FROM literature_screeningrevision")
            connection.execute("DELETE FROM literature_screeningdecision")
            connection.execute("DELETE FROM literature_paperkeywordassignment")
            connection.execute("DELETE FROM literature_discovery")
        with self.assertRaisesRegex(ps.ProjectStoreError, "unowned paper rows"):
            ps.save_copy_store(self.source.location, self.root / "unowned")
        self.assertFalse((self.root / "unowned").exists())

    def test_sqlite_exclusive_lock_and_real_unwritable_parent_fail_safely(self):
        locker = sqlite3.connect(self.db)
        self.addCleanup(locker.close)
        locker.execute("BEGIN EXCLUSIVE")

        def quick_ro(path):
            return sqlite3.connect(
                Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1
            )

        with (
            patch.object(ps, "_connect_ro", side_effect=quick_ro),
            self.assertRaises(ps.ProjectStoreError),
        ):
            ps.save_copy_store(self.source.location, self.root / "locked")
        self.assertFalse((self.root / "locked").exists())
        locker.rollback()
        if os.geteuid() == 0:
            self.skipTest(
                "root bypasses filesystem permission bits (SQLite lock path ran)")
        parent = self.root / "unwritable"
        parent.mkdir(mode=0o500)
        try:
            with self.assertRaises(ps.ProjectStoreError):
                ps.save_copy_store(self.source.location, parent / "copy")
            self.assertFalse((parent / "copy").exists())
        finally:
            parent.chmod(0o700)

    def test_real_http_manager_copy_close_backup_restore_and_csrf(self):
        """
        Exercise confirmed copy and unchanged recovery operations over HTTP.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        with (
            patch.dict(os.environ, {"WP_ROLE": "manager"}),
            override_settings(
                WP_ROLE="manager",
                SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
                SESSION_COOKIE_NAME="mgrsessionid",
                CSRF_COOKIE_NAME="mgrcsrftoken",
            ),
        ):
            server = make_server(
                "127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
            base = f"http://127.0.0.1:{server.server_port}"

            def post(action, source, destination="", *, csrf=True):
                """
                Submit a previewed copy or recovery operation.

                Args:
                    action: Requested store operation.
                    source: Synthetic source package.

                Kwargs:
                    destination: Destination path, defaulting to empty.
                    csrf: Send the real CSRF token; defaults to True.

                Returns:
                    tuple: HTTP status and response body.
                """
                fields = {
                    "action": action,
                    "source": str(source),
                    "destination": str(destination),
                    "confirm": "on",
                }
                headers = (
                    {"X-CSRFToken": next(c.value for c in jar if c.name ==
                                         "mgrcsrftoken")}
                    if csrf
                    else {}
                )
                if action == "copy" and csrf:
                    preview = urllib.request.Request(
                        base + "/workspace/operation/",
                        data=urllib.parse.urlencode(
                            fields | {"preview": "1"}).encode(),
                        headers=headers,
                    )
                    with opener.open(preview, timeout=60) as response:
                        body = response.read()
                    token = re.search(
                        rb'name="preview_token" value="([^"]+)"', body)
                    if token is None:
                        return response.status, body
                    fields["preview_token"] = token[1].decode()
                request = urllib.request.Request(
                    base + "/workspace/operation/",
                    data=urllib.parse.urlencode(fields).encode(),
                    headers=headers,
                )
                try:
                    with opener.open(request, timeout=60) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read()

            try:
                with opener.open(base + "/", timeout=20) as response:
                    body = response.read()
                for label in (b"Save Copy", b"Backup", b"Restore", b"Close worker"):
                    self.assertIn(label, body)
                self.assertEqual(
                    post("copy", self.source.location, self.root /
                         "http-copy", csrf=False)[0], 403
                )
                self.assertFalse((self.root / "http-copy").exists())
                status, _ = post("copy", self.source.location,
                                 self.root / "http-copy")
                self.assertEqual(status, 200)
                copied = ps.validate_package(self.root / "http-copy")
                self.assertNotEqual(copied.project_uuid,
                                    self.source.project_uuid)
                worker = ws.acquire_handle(self.source.location)
                status, body = post(
                    "backup", self.source.location, self.root / "http-backup")
                self.assertEqual(status, 200)
                self.assertIn(b"must be CLOSED", body)
                ws.release_handle(worker["handle_id"])
                self.assertEqual(post("close", self.source.location)[0], 200)
                self.assertIsNone(ws.find_worker(str(self.source.location)))
                self.assertEqual(
                    post("backup", self.source.location,
                         self.root / "http-backup")[0], 200
                )
                backup = ps.validate_package(self.root / "http-backup")
                with sqlite3.connect(self.db) as connection:
                    connection.execute(
                        "UPDATE literature_paper SET abstract='HTTP prior'")
                status, body = post(
                    "restore", backup.location, self.source.location)
                self.assertEqual(status, 200)
                self.assertIn(b"Previous project retained", body)
                self.assertEqual(self.rows(self.source.location),
                                 self.rows(backup.location))
                status, body = post(
                    "copy", self.source.location, backup.location)
                self.assertEqual(status, 200)
                self.assertIn(b"populated directory", body)
            finally:
                server.shutdown()
                thread.join(10)
                server.server_close()

    def test_open_worker_save_control_to_prefilled_manager_and_real_copy(self):
        """
        Confirm the manager copy preview without changing the worker source.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        class Tags(HTMLParser):
            def __init__(self, body):
                super().__init__()
                self.tags = []
                self.feed(body.decode())

            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, dict(attrs)))

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        with (
            patch.dict(os.environ, {"WP_ROLE": "manager"}),
            override_settings(
                WP_ROLE="manager",
                SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
                SESSION_COOKIE_NAME="mgrsessionid",
                CSRF_COOKIE_NAME="mgrcsrftoken",
            ),
        ):
            server = make_server(
                "127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
            before = self.rows(self.source.location)
            manifest_before = (self.source.location /
                               ps.MANIFEST_FILENAME).read_bytes()
            destination = self.root / "saved-from-header"
            try:
                with patch.dict(os.environ, {"WP_MANAGER_BASE": base}):
                    worker = ws.acquire_handle(self.source.location)
                worker_url = f"{worker['base']}{worker['prefix']}/project/{worker['project_id']}/"
                # Ordinary record query strings cannot select the copied DB or
                # redirect the new Save action to an untrusted manager.
                with opener.open(
                    worker_url + "?directory=/not-the-store&manager=https://attacker.invalid",
                    timeout=30,
                ) as response:
                    body = response.read()
                self.assertIn("Save project copy…".encode(), body)
                self.assertIn(b"Submitted edits are saved immediately", body)
                self.assertIn(b"Unsaved form drafts are not saved", body)
                self.assertIn(b"Backup / Restore / manage project", body)
                save = next(
                    attrs
                    for tag, attrs in Tags(body).tags
                    if tag == "a"
                    and attrs.get("class") == "button-link"
                    and "#project-operations" in attrs.get("href", "")
                )
                # Native new-tab navigation keeps the original draft/document
                # and lease; it does not invoke the existing Exit/discard path.
                self.assertEqual(save["target"], "_blank")
                self.assertEqual(save["rel"], "noopener")
                self.assertNotIn("data-project-exit", save)
                selected = urllib.parse.urlsplit(save["href"])
                self.assertEqual(
                    selected.netloc, urllib.parse.urlsplit(base).netloc)
                query = urllib.parse.parse_qs(selected.query)
                self.assertEqual(query["directory"], [
                                 str(self.source.location)])
                self.assertEqual(query["operation"], ["copy"])
                # Navigation is not destination selection or approval, even if
                # a caller forges those GET parameters.
                preview_url = selected._replace(
                    query=selected.query
                    + "&"
                    + urllib.parse.urlencode(
                        {
                            "destination": str(destination),
                            "confirm": "on",
                        }
                    )
                ).geturl()
                with opener.open(preview_url, timeout=30) as response:
                    form_body = response.read()
                tags = Tags(form_body).tags
                source = next(
                    attrs for tag, attrs in tags if tag == "input" and attrs.get("name") == "source"
                )
                dest = next(
                    attrs
                    for tag, attrs in tags
                    if tag == "input" and attrs.get("name") == "destination"
                )
                confirm = next(
                    attrs
                    for tag, attrs in tags
                    if tag == "input" and attrs.get("name") == "confirm"
                )
                self.assertEqual(source["value"], str(self.source.location))
                self.assertEqual(dest.get("value", ""), "")
                self.assertNotIn("checked", confirm)
                self.assertTrue(
                    any(
                        tag == "option" and attrs.get(
                            "value") == "copy" and "selected" in attrs
                        for tag, attrs in tags
                    )
                )
                self.assertFalse(destination.exists())
                self.assertEqual(self.rows(self.source.location), before)
                self.assertIsNotNone(ws.heartbeat_handle(worker["handle_id"]))

                token = next(c.value for c in jar if c.name == "mgrcsrftoken")
                fields = {
                    "action": "copy",
                    "source": source["value"],
                    "destination": str(destination),
                }

                def post(values):
                    """
                    Submit the manager operation with its real CSRF token.

                    Args:
                        values: Form fields for this request.

                    Kwargs:
                        None

                    Returns:
                        bytes: Rendered response after redirects.
                    """
                    request = urllib.request.Request(
                        base + "/workspace/operation/",
                        data=urllib.parse.urlencode(values).encode(),
                        headers={"X-CSRFToken": token},
                    )
                    with opener.open(request, timeout=30) as response:
                        return response.read()

                preview = post(fields)  # missing confirmation cannot copy
                self.assertFalse(destination.exists())
                post(fields | {"destination": "", "confirm": "on"})
                self.assertFalse(destination.exists())
                preview_token = next(
                    attrs["value"] for tag, attrs in Tags(preview).tags
                    if tag == "input" and attrs.get("name") == "preview_token"
                    and attrs.get("value")
                )
                post(fields | {
                    "confirm": "on", "preview_token": preview_token,
                })
                copied = ps.validate_package(destination)
                self.assertNotEqual(copied.project_uuid,
                                    self.source.project_uuid)
                self.assertEqual(self.rows(destination), before)
                self.assertEqual(self.rows(self.source.location), before)
                self.assertEqual(
                    (self.source.location /
                     ps.MANIFEST_FILENAME).read_bytes(), manifest_before
                )
                self.assertIsNotNone(ws.heartbeat_handle(worker["handle_id"]))
            finally:
                server.shutdown()
                thread.join(10)
                server.server_close()

    def test_manager_prefill_refuses_unrecognised_source_and_unsafe_action(self):
        with (
            patch.dict(os.environ, {"WP_ROLE": "manager"}),
            override_settings(
                SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies"),
        ):
            client = Client()
            response = client.get(
                "/workspace/", {"directory": str(self.root /
                                                 "missing"), "operation": "copy"}
            )
            self.assertContains(response, "Cannot preselect this project")
            self.assertIsNone(
                response.context["operation_form"]["source"].value())
            response = client.get(
                "/workspace/", {"directory": str(self.source.location),
                                "operation": "delete"}
            )
            self.assertIsNone(
                response.context["operation_form"]["source"].value())


class SaveControlBoundaryTests(SimpleTestCase):
    def test_only_configured_loopback_manager_origin_is_accepted(self):
        for base in (
            "https://attacker.invalid",
            "http://127.0.0.1:8001/path",
            "http://user@127.0.0.1:8001",
            "http://127.0.0.1:8001?source=x",
            "http://127.0.0.1:99999",
            "",
        ):
            with self.subTest(base=base), patch.dict(os.environ, {"WP_MANAGER_BASE": base}):
                self.assertEqual(manager_control_base(), "")
        with patch.dict(os.environ, {"WP_MANAGER_BASE": "http://127.0.0.1:8001/"}):
            self.assertEqual(manager_control_base(), "http://127.0.0.1:8001")


# --- Shared legacy papers must not expose another project's private exports ---
class MarkdownPrivacyTests(TestCase):
    def test_legacy_open_project_explains_saved_edits_and_portable_save_limitation(self):
        project = Project.objects.create(name="Synthetic legacy project")
        project.ensure_default_stages()
        with patch.dict(os.environ, {"WP_ROLE": "developer"}):
            response = self.client.get(f"/project/{project.pk}/")
        self.assertContains(response, "Legacy shared-DB mode")
        self.assertContains(
            response, "submitted edits are saved immediately to the shared developer database"
        )
        self.assertContains(response, "Save / project folders")
        self.assertContains(response, "New / Load manager")
        self.assertContains(response, "python3.12 launch.py --port 8001")
        self.assertContains(response, "explicit developer / rollback mode")
        self.assertContains(response, "does not determine conversion status")
        self.assertNotContains(
            response, "Conversion / adoption is pending operator action")
        self.assertNotContains(response, "Save project copy…</a>")

    def test_shared_paper_notes_provenance_and_keyword_scope(self):
        a = Project.objects.create(name="A")
        b = Project.objects.create(name="PRIVATE PROJECT B")
        a.ensure_default_stages()
        b.ensure_default_stages()
        sa = Search.objects.create(
            project=a, name="A search", keywords=["A query"])
        sb = Search.objects.create(
            project=b, name="PRIVATE SEARCH B", keywords=["PRIVATE QUERY B"])
        paper = Paper.objects.create(
            title="Shared", keywords=["PRIVATE GLOBAL KEYWORD"], raw={"private": "PRIVATE RAW"}
        )
        Discovery.objects.create(
            search=sa, paper=paper, source="arxiv", query="A query")
        Discovery.objects.create(
            search=sb, paper=paper, source="semantic_scholar", query="PRIVATE QUERY B"
        )
        ScreeningDecision.objects.create(
            paper=paper, stage=a.stages.first(), decision="ACCEPT", notes="A notes"
        )
        ScreeningDecision.objects.create(
            paper=paper, stage=b.stages.first(), decision="REJECT", notes="PRIVATE B NOTES"
        )
        PaperKeywordAssignment.objects.create(
            paper=paper, project=b, search=sb, keyword="PRIVATE B KEYWORD"
        )
        first = build_markdown(a)
        self.assertIn("A notes", first)
        self.assertIn("A query", first)
        self.assertNotIn("PRIVATE", first)
        PaperKeywordAssignment.objects.create(
            paper=paper, project=a, search=sa, keyword="A keyword"
        )
        second = build_markdown(a)
        self.assertIn("A keyword", second)
        self.assertNotIn("PRIVATE", second)
        self.assertIn("not a reloadable store", second)
