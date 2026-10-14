#!/usr/bin/env python3

"""
Test fixed-default worker isolation using synthetic stores.

WP01.01 spike: two fixed-default workers stay isolated (synthetic stores).

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_wp0101_worker_boundary.py

@Brief:  Test fixed-default worker isolation using synthetic stores.

@Date:   03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# @File: test_wp0101_worker_boundary.py
# @Date: 03/10/2026

# Regular library imports
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# Custom library Imports
from django.conf import settings
from django.test import SimpleTestCase

# ------------------------------------------------------------------------ #
# Worker entry point (runs in a child process with its own store)
# ------------------------------------------------------------------------ #

# ponytail: embedded worker script; a real launcher module arrives in WP01.10.
_WORKER_SCRIPT = """
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.management import call_command
from django.db import transaction

from literature.models import Paper, Project

call_command("migrate", run_syncdb=True, verbosity=0)
mode, marker = sys.argv[1], sys.argv[2]
if mode == "seed":
    project = Project.objects.create(name="Worker project", description=marker)
    project.ensure_default_stages()
    paper = Paper.objects.create(title="Shared title", abstract=marker)
    print(f"OK project={project.pk} paper={paper.pk}", flush=True)
elif mode == "add":
    paper = Paper.objects.create(title=f"Second paper {marker}", abstract=marker)
    print(f"OK paper={paper.pk}", flush=True)
elif mode == "fail":
    try:
        with transaction.atomic():
            Paper.objects.create(title="Will roll back A", abstract=marker)
            Paper.objects.create(title="Will roll back B", abstract=marker)
            raise RuntimeError("WP01.01 deliberate failure")
    except RuntimeError:
        pass
    print(f"OK papers={Paper.objects.count()}", flush=True)
"""


# ------------------------------------------------------------------------ #
# Proof tests
# ------------------------------------------------------------------------ #


class WorkerBoundaryTests(SimpleTestCase):
    """
    Two processes, each with an immutable default store, cannot cross.
    """

    # --- Child worker plumbing ---
    def _run_worker(self, store: Path, mode: str, marker: str) -> str:
        """
        Run one worker bound to ``store``; return its stdout.

        Args:
            store (Path): Synthetic SQLite file bound as the default DB.
            mode (str): Seed a project, add a paper, or fail a transaction.
            marker (str): Abstract marker distinguishing the worker's rows.

        Kwargs:
            None

        Returns:
            str: Stripped stdout from the successful worker process.

        Raises:
            AssertionError: If the worker exits unsuccessfully.
            subprocess.TimeoutExpired: If the worker exceeds 180 seconds.
        """
        base = Path(settings.BASE_DIR)
        env = dict(os.environ, LOCAL_SURVEY_DB=str(store))
        completed = subprocess.run(
            [sys.executable, "-c", _WORKER_SCRIPT, mode, marker],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=base,
            env=env,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"worker {mode} failed: {completed.stderr[-2000:]}",
        )
        return completed.stdout.strip()

    # --- Row readers (parent inspects synthetic stores only) ---
    @staticmethod
    def _papers(store: Path) -> list[tuple]:
        """
        Read ordered paper identities and content from a synthetic store.

        Args:
            store (Path): Synthetic SQLite file to inspect.

        Kwargs:
            None

        Returns:
            list[tuple]: Paper ID, title and abstract rows ordered by ID.
        """
        with sqlite3.connect(store) as connection:
            return list(
                connection.execute(
                    "SELECT id, title, abstract FROM literature_paper ORDER BY id"
                )
            )

    @staticmethod
    def _tables(store: Path) -> set[str]:
        """
        Read table names from a synthetic SQLite file.

        Args:
            store (Path): Synthetic store or registry file to inspect.

        Kwargs:
            None

        Returns:
            set[str]: Names of all tables in the file.
        """
        with sqlite3.connect(store) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

    # --- Isolation with overlapping IDs and interleaved saves ---
    def test_overlapping_ids_and_interleaved_saves_stay_isolated(self) -> None:
        """
        Keep distinct abstracts when worker IDs and titles overlap.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0101-workers-") as tmp:
            store_a = Path(tmp) / "a.sqlite3"
            store_b = Path(tmp) / "b.sqlite3"
            self._run_worker(store_a, "seed", "marker-A")
            self._run_worker(store_b, "seed", "marker-B")
            # Interleaved second saves: A, then B, then A again is unnecessary;
            # one alternating round each way proves no cross-talk.
            self._run_worker(store_a, "add", "marker-A")
            self._run_worker(store_b, "add", "marker-B")

            papers_a = self._papers(store_a)
            papers_b = self._papers(store_b)
            # Local row IDs overlap (both stores start at 1) yet contents differ.
            self.assertEqual([row[0] for row in papers_a], [1, 2])
            self.assertEqual([row[0] for row in papers_b], [1, 2])
            self.assertTrue(all(row[2] == "marker-A" for row in papers_a))
            self.assertTrue(all(row[2] == "marker-B" for row in papers_b))
            # same shared title
            self.assertEqual(papers_a[0][1], papers_b[0][1])

    # --- Failed default-alias transaction rolls back only its own store ---
    def test_failed_transaction_rolls_back_only_own_store(self) -> None:
        """
        Roll back a worker's partial pair without changing the other store.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0101-fail-") as tmp:
            store_c = Path(tmp) / "c.sqlite3"
            store_d = Path(tmp) / "d.sqlite3"
            self._run_worker(store_c, "seed", "marker-C")
            self._run_worker(store_d, "seed", "marker-D")
            output = self._run_worker(store_c, "fail", "marker-C")
            # seed row survives, partial pair gone
            self.assertIn("papers=1", output)
            self.assertEqual(len(self._papers(store_c)), 1)
            self.assertEqual(len(self._papers(store_d)), 1)
            self.assertEqual(self._papers(store_d)[0][2], "marker-D")

    # --- Parent/control registry never receives research records ---
    def test_parent_registry_receives_no_research_rows(self) -> None:
        """
        Leave the registry schema unchanged when workers seed their stores.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory(prefix="wp0101-registry-") as tmp:
            registry = Path(tmp) / "registry.sqlite3"
            with sqlite3.connect(registry) as connection:
                connection.execute(
                    "CREATE TABLE worker_registry (store_path TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT INTO worker_registry (store_path) VALUES (?)",
                    (str(Path(tmp) / "a.sqlite3"),),
                )
                connection.commit()
            before = self._tables(registry)

            store_a = Path(tmp) / "a.sqlite3"
            store_b = Path(tmp) / "b.sqlite3"
            self._run_worker(store_a, "seed", "marker-A")
            self._run_worker(store_b, "seed", "marker-B")

            after = self._tables(registry)
            self.assertEqual(before, after)
            self.assertFalse(
                {name for name in after if name.startswith("literature_")},
                "research tables leaked into the parent registry",
            )
            self.assertTrue(
                "literature_paper" in self._tables(store_a),
                "worker store A must own the research tables",
            )

    # --- Cookie defaults confirm the port-sharing hazard (decision input) ---
    def test_cookie_defaults_confirm_port_sharing_hazard(self) -> None:
        """
        Record legacy cookie defaults and the single default DB alias.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        # ponytail: documents today's hazard; WP01.03 must give workers
        # non-colliding session/CSRF names or paths.
        self.assertEqual(settings.SESSION_COOKIE_NAME, "sessionid")
        self.assertEqual(settings.CSRF_COOKIE_NAME, "csrftoken")
        self.assertEqual(settings.SESSION_COOKIE_PATH, "/")
        self.assertEqual(len(settings.DATABASES), 1)
        self.assertIn("default", settings.DATABASES)
