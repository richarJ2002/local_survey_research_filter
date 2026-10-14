#!/usr/bin/env python3

"""Test versioned project-store creation, loading and refusal boundaries.

WP01.02: versioned project-store create/open/refusal checks (synthetic).

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_wp0102_project_store.py

@Brief:  Test versioned project-store creation, loading and refusal boundaries.

@Date:   03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# @File: test_wp0102_project_store.py
# @Date: 03/10/2026

# Regular library imports
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# Custom library Imports
from django.conf import settings
from django.test import SimpleTestCase

from literature.project_store import (
    ProjectStoreError,
    backup_store_db,
    create_project_store,
    init_app_registry,
    inspect_project_store,
    open_project_store,
    schema_status,
)

# ------------------------------------------------------------------------ #
# Worker entry points (children bind their own fixed default store)
# ------------------------------------------------------------------------ #

# ponytail: embedded worker scripts; a real launcher module arrives in WP01.10.
_ADD_PAPER_SCRIPT = """
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from literature.models import Paper

paper = Paper.objects.create(title="WP01.02 paper", abstract="synthetic")
print(f"OK paper={paper.pk}", flush=True)
"""

_ADD_PROJECT_SCRIPT = """
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from literature.models import Project

project = Project.objects.create(name="Second project")
print(f"OK project={project.pk}", flush=True)
"""


# ------------------------------------------------------------------------ #
# Proof tests (synthetic temp stores only; live DB never written)
# ------------------------------------------------------------------------ #


class ProjectStoreTests(SimpleTestCase):
    """One folder holds exactly one valid, independently editable project."""

    # --- Child worker plumbing ---
    def _run_worker(self, store_db: Path, script: str) -> str:
        """Run a child script with its default DB fixed to a synthetic store.

        Args:
            store_db (Path): Synthetic SQLite file for the child process.
            script (str): Python source to execute in the child.

        Kwargs:
            None

        Returns:
            str: Stripped stdout from the successful child process.

        Raises:
            AssertionError: If the child exits unsuccessfully.
            subprocess.TimeoutExpired: If the child exceeds 300 seconds.
        """
        base = Path(settings.BASE_DIR)
        env = dict(os.environ, LOCAL_SURVEY_DB=str(store_db))
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
            cwd=base,
            env=env,
        )
        self.assertEqual(completed.returncode, 0,
                         f"worker failed: {completed.stderr[-2000:]}")
        return completed.stdout.strip()

    # --- Read-only row readers over synthetic stores (quoting-safe URIs) ---
    @staticmethod
    def _papers(store_db: Path) -> list[tuple]:
        """Read synthetic paper IDs and titles through a read-only URI.

        Args:
            store_db (Path): SQLite file whose path is URI-quoted.

        Kwargs:
            None

        Returns:
            list[tuple]: Paper ID and title rows ordered by ID.
        """
        uri = Path(store_db).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            return list(connection.execute("SELECT id, title FROM literature_paper ORDER BY id"))

    # --- Create / open / reopen retains data and identity ---
    def test_create_open_reopen_retains_data_and_identity(self) -> None:
        """Retain the project UUID and a child-added paper after reopening.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-store-") as tmp:
            location = Path(tmp) / "survey-a"
            created = create_project_store(location, "Survey A")
            opened = open_project_store(location)
            self.assertEqual(opened.project_uuid, created.project_uuid)

            self._run_worker(location / "project.sqlite3", _ADD_PAPER_SCRIPT)
            reopened = open_project_store(location)
            self.assertEqual(reopened.project_uuid, created.project_uuid)
            self.assertEqual(self._papers(
                location / "project.sqlite3"), [(1, "WP01.02 paper")])
            report = inspect_project_store(location)
            self.assertTrue(report["complete"])
            self.assertEqual(report["project_count"], 1)

    # --- New refuses populated destinations and never wipes them ---
    def test_new_refuses_populated_destinations(self) -> None:
        """Refuse populated directories and file targets without wiping data.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-new-") as tmp:
            populated = Path(tmp) / "populated"
            populated.mkdir()
            sentinel = populated / "keep.txt"
            sentinel.write_text("user data", encoding="utf-8")
            with self.assertRaises(ProjectStoreError):
                create_project_store(populated, "Nope")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data")

            as_file = Path(tmp) / "afile"
            as_file.write_text("x", encoding="utf-8")
            with self.assertRaises(ProjectStoreError):
                create_project_store(as_file, "Nope")

    # --- Load refuses every bad shape without creating or migrating ---
    def test_load_refuses_bad_stores(self) -> None:
        """Reject missing, malformed, unsupported and path-traversing stores.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-load-") as tmp:
            with self.assertRaises(ProjectStoreError):
                open_project_store(Path(tmp) / "missing")

            no_manifest = Path(tmp) / "no-manifest"
            no_manifest.mkdir()
            with self.assertRaises(ProjectStoreError):
                open_project_store(no_manifest)

            corrupt = Path(tmp) / "corrupt"
            corrupt.mkdir()
            (corrupt /
             "project.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(ProjectStoreError):
                open_project_store(corrupt)

            bad_uuid = Path(tmp) / "bad-uuid"
            bad_uuid.mkdir()
            (bad_uuid / "project.json").write_text(
                json.dumps(
                    {"format_version": 1, "project_uuid": "nope",
                        "database": "project.sqlite3"}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ProjectStoreError):
                open_project_store(bad_uuid)

            for version in (0, 999):
                pending = Path(tmp) / f"v{version}"
                pending.mkdir()
                (pending / "project.json").write_text(
                    json.dumps(
                        {
                            "format_version": version,
                            "project_uuid": "12345678-1234-5678-1234-567812345678",
                            "database": "project.sqlite3",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(ProjectStoreError):
                    open_project_store(pending)
                self.assertFalse((pending / "project.sqlite3").exists())

            traversal = Path(tmp) / "traversal"
            traversal.mkdir()
            (traversal / "project.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "project_uuid": "12345678-1234-5678-1234-567812345678",
                        "database": "../evil.sqlite3",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ProjectStoreError):
                open_project_store(traversal)

    # --- Identity, count and drift refusals on real (synthetic) stores ---
    def test_open_refuses_identity_and_count_problems(self) -> None:
        """Reject mismatched manifest UUIDs and multiple stored projects.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-identity-") as tmp:
            location = Path(tmp) / "store"
            created = create_project_store(location, "Identity")

            manifest_path = location / "project.json"
            doc = json.loads(manifest_path.read_text(encoding="utf-8"))
            doc["project_uuid"] = "12345678-1234-5678-1234-567812345678"
            manifest_path.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)
            # Restore the true manifest; the store itself was never mutated.
            doc["project_uuid"] = str(created.project_uuid)
            manifest_path.write_text(json.dumps(doc), encoding="utf-8")
            open_project_store(location)

            store_db = location / "project.sqlite3"
            self._run_worker(store_db, _ADD_PROJECT_SCRIPT)
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)

    # --- Empty and unmigrated databases are incomplete, not loadable ---
    def test_open_refuses_empty_project_table(self) -> None:
        """Reject and diagnose a store whose project rows were removed.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-empty-") as tmp:
            location = Path(tmp) / "store"
            create_project_store(location, "Empty")
            store_db = location / "project.sqlite3"
            with sqlite3.connect(store_db) as connection:
                connection.execute("DELETE FROM literature_screeningrevision")
                connection.execute("DELETE FROM literature_stage")
                connection.execute("DELETE FROM literature_project")
                connection.commit()
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)
            report = inspect_project_store(location)
            self.assertFalse(report["complete"])

    # --- Migration history and FK integrity ride with the store ---
    def test_schema_history_and_fk_integrity(self) -> None:
        """Check migration history, foreign keys and SQLite integrity.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-schema-") as tmp:
            location = Path(tmp) / "store"
            create_project_store(location, "Schema")
            store_db = location / "project.sqlite3"
            status = schema_status(store_db)
            self.assertTrue(
                status.in_sync, f"drift: {status.missing}/{status.extra}")
            self.assertIn("0012_surveyentry_inclusion_origin", status.applied)
            uri = store_db.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                self.assertEqual(
                    list(connection.execute("PRAGMA foreign_key_check")), [])
                self.assertEqual(connection.execute(
                    "PRAGMA integrity_check").fetchone(), ("ok",))

    # --- Stores, default DB and registry stay mutually untouched ---
    def test_no_writes_to_other_default_or_registry(self) -> None:
        """Keep the other store, default DB and registry unchanged by saves.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-isolation-") as tmp:
            registry = Path(tmp) / "registry.sqlite3"
            init_app_registry(registry)
            before_tables = self._tables(registry)
            default_db = Path(settings.DATABASES["default"]["NAME"])
            default_stat = default_db.stat() if default_db.exists() else None

            store_a = Path(tmp) / "a"
            store_b = Path(tmp) / "b"
            create_project_store(store_a, "A")
            create_project_store(store_b, "B")
            self._run_worker(store_a / "project.sqlite3", _ADD_PAPER_SCRIPT)

            self.assertEqual(len(self._papers(store_a / "project.sqlite3")), 1)
            self.assertEqual(self._papers(store_b / "project.sqlite3"), [])
            self.assertEqual(self._tables(registry), before_tables)
            if default_stat is None:
                self.assertFalse(default_db.exists(),
                                 "default DB must not be created")
            else:
                self.assertEqual(default_db.stat().st_mtime_ns,
                                 default_stat.st_mtime_ns)
                self.assertEqual(default_db.stat().st_size,
                                 default_stat.st_size)

    # --- No credential or session rows travel inside research packages ---
    def test_no_credentials_or_sessions_in_package(self) -> None:
        """Exclude credential tables and populated user or session rows.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-secrets-") as tmp:
            location = Path(tmp) / "store"
            create_project_store(location, "Secrets")
            store_db = location / "project.sqlite3"
            uri = store_db.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            leaked = {
                name
                for name in tables
                if any(token in name for token in ("credential", "secret", "token", "authtoken"))
            }
            self.assertEqual(leaked, set())
            with sqlite3.connect(uri, uri=True) as connection:
                if "django_session" in tables:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM django_session").fetchone(),
                        (0,),
                    )
                if "auth_user" in tables:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM auth_user").fetchone(),
                        (0,),
                    )
            manifest_doc = json.loads(
                (location / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(
                set(manifest_doc), {"format_version",
                                    "project_uuid", "database", "created_at"}
            )

    # --- Interrupted creation is refused, diagnosed and left intact ---
    def test_interrupted_creation_is_refused_and_diagnosed(self) -> None:
        """Preserve a partial manifest and report its missing database.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-partial-") as tmp:
            partial = Path(tmp) / "partial"
            partial.mkdir()
            manifest_doc = {
                "format_version": 1,
                "project_uuid": "12345678-1234-5678-1234-567812345678",
                "database": "project.sqlite3",
                "created_at": "2026-10-03T00:00:00+00:00",
            }
            manifest_text = json.dumps(manifest_doc, indent=2) + "\n"
            (partial / "project.json").write_text(manifest_text, encoding="utf-8")
            with self.assertRaises(ProjectStoreError):
                create_project_store(partial, "Nope")
            # ponytail: refusal must not destroy the pre-existing files.
            self.assertEqual(
                (partial / "project.json").read_text(encoding="utf-8"), manifest_text)
            report = inspect_project_store(partial)
            self.assertFalse(report["complete"])
            self.assertIn("database_error", report)

    # --- Backup snapshots stay restorable without touching the source ---
    def test_backup_snapshot_is_restorable(self) -> None:
        """Restore a snapshot with the original project UUID and paper row.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-backup-") as tmp:
            location = Path(tmp) / "store"
            created = create_project_store(location, "Backup")
            store_db = location / "project.sqlite3"
            self._run_worker(store_db, _ADD_PAPER_SCRIPT)
            snapshot = backup_store_db(store_db, location / "backups")
            self.assertTrue(snapshot.is_file() and snapshot.stat().st_size > 0)

            restored = Path(tmp) / "restored"
            restored.mkdir()
            (restored / "project.json").write_text(
                (location / "project.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            shutil.copy(snapshot, restored / "project.sqlite3")
            reopened = open_project_store(restored)
            self.assertEqual(reopened.project_uuid, created.project_uuid)
            self.assertEqual(self._papers(
                restored / "project.sqlite3"), [(1, "WP01.02 paper")])

    # --- Registry helper creates only the frozen private schema ---
    def test_registry_holds_no_research_tables(self) -> None:
        """Limit the private registry to project, worker and lease tables.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-registry-") as tmp:
            registry = init_app_registry(
                Path(tmp) / "app" / "registry.sqlite3")
            # ponytail (WP01.05): the private registry gained a leases runtime
            # table for tab handles; the invariant is no research tables here.
            tables = self._tables(registry)
            self.assertEqual(tables, {"projects", "workers", "leases"})
            self.assertFalse(
                {t for t in tables if t.startswith("literature_")})

    # --- Manifest version needs the exact int 1, not True/1.0/"1" ---
    def test_manifest_version_requires_exact_int(self) -> None:
        """Reject boolean, floating-point and string manifest versions.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-version-") as tmp:
            for index, version in enumerate((True, 1.0, "1")):
                location = Path(tmp) / f"v{index}"
                location.mkdir()
                (location / "project.json").write_text(
                    json.dumps(
                        {
                            "format_version": version,
                            "project_uuid": "12345678-1234-5678-1234-567812345678",
                            "database": "project.sqlite3",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(ProjectStoreError):
                    open_project_store(location)
                report = inspect_project_store(location)
                self.assertFalse(report["complete"])
                self.assertIn("manifest_error", report)

    # --- Applied migration names must equal on-disk names in both directions ---
    def test_schema_status_rejects_extra_and_missing(self) -> None:
        """Reject both unknown and missing applied migration names.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-drift-") as tmp:
            location = Path(tmp) / "store"
            create_project_store(location, "Drift")
            store_db = location / "project.sqlite3"
            with sqlite3.connect(store_db) as connection:
                connection.execute(
                    "INSERT INTO django_migrations (app, name, applied) VALUES (?, ?, ?)",
                    ("literature", "9999_from_the_future", "2026-10-03 00:00:00"),
                )
                connection.commit()
            status = schema_status(store_db)
            self.assertFalse(status.in_sync)
            self.assertEqual(status.extra, ("9999_from_the_future",))
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)
            self.assertFalse(inspect_project_store(location)["complete"])

            with sqlite3.connect(store_db) as connection:
                connection.execute(
                    "DELETE FROM django_migrations WHERE app = 'literature' AND name = ?",
                    ("9999_from_the_future",),
                )
                connection.execute(
                    "DELETE FROM django_migrations WHERE app = 'literature' AND name = ?",
                    ("0012_surveyentry_inclusion_origin",),
                )
                connection.commit()
            status = schema_status(store_db)
            self.assertFalse(status.in_sync)
            self.assertEqual(
                status.missing, ("0012_surveyentry_inclusion_origin",))
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)

    # --- Identity table needs exactly one row with UUID and version ---
    def test_identity_requires_single_matching_row(self) -> None:
        """Reject extra identity rows and versions until the identity matches.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-idrows-") as tmp:
            location = Path(tmp) / "store"
            created = create_project_store(location, "Identity rows")
            store_db = location / "project.sqlite3"
            with sqlite3.connect(store_db) as connection:
                connection.execute(
                    "INSERT INTO wp_store_identity (project_uuid, format_version)"
                    " VALUES (?, ?)",
                    ("12345678-1234-5678-1234-567812345678", 1),
                )
                connection.commit()
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)
            self.assertFalse(inspect_project_store(location)["complete"])

            with sqlite3.connect(store_db) as connection:
                connection.execute(
                    "DELETE FROM wp_store_identity WHERE project_uuid = ?",
                    ("12345678-1234-5678-1234-567812345678",),
                )
                connection.execute(
                    "UPDATE wp_store_identity SET format_version = 999")
                connection.commit()
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)

            with sqlite3.connect(store_db) as connection:
                connection.execute(
                    "UPDATE wp_store_identity SET format_version = 1")
                connection.commit()
            self.assertEqual(
                open_project_store(location).project_uuid, created.project_uuid
            )

    # --- Valid Linux ?/#/% names must open through the quoted-URI readers ---
    def test_special_characters_in_store_path(self) -> None:
        """Open, inspect and back up a store with URI-sensitive path characters.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-weird-") as tmp:
            location = Path(tmp) / "we?ird #%name"
            created = create_project_store(location, "Weird")
            self._run_worker(location / "project.sqlite3", _ADD_PAPER_SCRIPT)
            reopened = open_project_store(location)
            self.assertEqual(reopened.project_uuid, created.project_uuid)
            self.assertEqual(self._papers(
                location / "project.sqlite3"), [(1, "WP01.02 paper")])
            self.assertTrue(inspect_project_store(location)["complete"])
            snapshot = backup_store_db(
                location / "project.sqlite3", location / "backups")
            self.assertTrue(snapshot.is_file())

    # --- Garbage and truncated databases fail wrapped, never raw ---
    def test_corrupt_and_truncated_db_refused(self) -> None:
        """Wrap refusals for garbage, invalid headers and truncated databases.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-corrupt-") as tmp:
            for name, payload in (
                ("garbage", b"not a database at all, just text........"),
                ("short", b"SQLite format 3\x00_SHORT"),
            ):
                location = Path(tmp) / name
                location.mkdir()
                (location / "project.json").write_text(
                    json.dumps(
                        {
                            "format_version": 1,
                            "project_uuid": "12345678-1234-5678-1234-567812345678",
                            "database": "project.sqlite3",
                        }
                    ),
                    encoding="utf-8",
                )
                (location / "project.sqlite3").write_bytes(payload)
                with self.assertRaises(ProjectStoreError):
                    open_project_store(location)
                report = inspect_project_store(location)
                self.assertFalse(report["complete"])
                self.assertIn("database_error", report)

            location = Path(tmp) / "truncated"
            create_project_store(location, "Truncated")
            store_db = location / "project.sqlite3"
            with open(store_db, "r+b") as handle:
                handle.truncate(50)
            with self.assertRaises(ProjectStoreError):
                open_project_store(location)
            self.assertFalse(inspect_project_store(location)["complete"])

    # --- Backup never creates a missing source ---
    def test_backup_refuses_missing_source(self) -> None:
        """Refuse a missing backup source without creating files or directories.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-nobackup-") as tmp:
            missing = Path(tmp) / "absent.sqlite3"
            target_dir = Path(tmp) / "backups"
            with self.assertRaises(ProjectStoreError):
                backup_store_db(missing, target_dir)
            self.assertFalse(missing.exists())
            self.assertFalse(target_dir.exists())

    # --- A held claim blocks a second creator without touching files ---
    def test_claim_blocks_second_creator(self) -> None:
        """Refuse creation while a claim exists and allow it after removal.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-claim-") as tmp:
            location = Path(tmp) / "store"
            location.mkdir()
            claim = location / ".project-claim.lock"
            claim.write_text(json.dumps(
                {"pid": os.getpid()}), encoding="utf-8")
            with self.assertRaises(ProjectStoreError):
                create_project_store(location, "Blocked")
            self.assertEqual(
                [entry.name for entry in location.iterdir()], [claim.name])
            claim.unlink()
            created = create_project_store(location, "Unblocked")
            self.assertEqual(open_project_store(
                location).project_uuid, created.project_uuid)
            self.assertFalse((location / ".project-claim.lock").exists())

    # --- Two racing creators: exactly one wins, the store stays valid ---
    def test_concurrent_creation_single_winner(self) -> None:
        """Allow one racing creator and leave a valid store without a claim.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-race-") as tmp:
            location = Path(tmp) / "store"
            outcomes: list[str] = []

            def attempt() -> None:
                """Record whether this racing store-creation attempt succeeds.

                Args:
                    None

                Kwargs:
                    None

                Returns:
                    None
                """
                try:
                    create_project_store(location, "Raced")
                    outcomes.append("created")
                except ProjectStoreError:
                    outcomes.append("refused")

            first, second = threading.Thread(
                target=attempt), threading.Thread(target=attempt)
            first.start()
            second.start()
            first.join(timeout=600)
            second.join(timeout=600)
            self.assertEqual(sorted(outcomes), ["created", "refused"])
            opened = open_project_store(location)
            with sqlite3.connect(location / "project.sqlite3") as connection:
                rows = list(connection.execute(
                    "SELECT project_uuid FROM wp_store_identity"))
            self.assertEqual([row[0] for row in rows], [
                             str(opened.project_uuid)])
            self.assertFalse((location / ".project-claim.lock").exists())

    # --- Canonical location resolves symlinks; file identity keys dedup ---
    def test_canonical_location_and_db_identity(self) -> None:
        """Resolve symlink locations and retain device-and-inode DB identity.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0102-canonical-") as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real, target_is_directory=True)
            created = create_project_store(link / "store", "Linked")
            self.assertEqual(created.location, (real / "store").resolve())
            file_stat = (real / "store" / "project.sqlite3").stat()
            self.assertEqual(created.db_identity,
                             (file_stat.st_dev, file_stat.st_ino))
            opened = open_project_store(link / "store")
            self.assertEqual(opened.location, created.location)
            self.assertEqual(opened.db_identity, created.db_identity)
            report = inspect_project_store(link / "store")
            self.assertTrue(report["complete"])
            self.assertEqual(report["location"], str(created.location))

    # --- Table-name reader for the private registry ---
    @staticmethod
    def _tables(database: Path) -> set[str]:
        """Read table names from the synthetic private registry.

        Args:
            database (Path): Synthetic SQLite registry file to inspect.

        Kwargs:
            None

        Returns:
            set[str]: Names of all tables in the registry.
        """
        with sqlite3.connect(database) as connection:
            return {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
