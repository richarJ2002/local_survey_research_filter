"""Preserve saved project names without bypassing the explicit schema gate.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_workspace_schema_name.py

@Brief:  Preserve saved project names without bypassing the explicit schema gate.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Custom library Imports
from django.test import SimpleTestCase

from literature import workspace
from literature.project_store import ProjectStoreError, create_project_store

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class WorkspaceSchemaNameTests(SimpleTestCase):
    """Use an isolated old-schema package; never use real survey records."""

    def test_saved_name_survives_schema_refusal_without_writes(self) -> None:
        """Display the old project's actual name while keeping Load refused.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {
                "XDG_DATA_HOME": str(root / "state"),
                "PYTHON_DOTENV_DISABLED": "1",
            }):
                store = create_project_store(root / "project", "Saved survey")
                database = store.location / "project.sqlite3"
                # Only this empty synthetic fixture is put at the older schema.
                code = (
                    "import django; django.setup(); "
                    "from django.db import connection; "
                    "from django.db.migrations.executor import MigrationExecutor; "
                    "MigrationExecutor(connection).migrate([('literature', "
                    "'0012_surveyentry_inclusion_origin')])"
                )
                env = dict(os.environ, LOCAL_SURVEY_DB=str(database),
                           WP_ROLE="developer",
                           DJANGO_SETTINGS_MODULE="config.settings")
                subprocess.run([sys.executable, "-c", code], env=env,
                               check=True, capture_output=True, timeout=30)
                before = hashlib.sha256(database.read_bytes()).hexdigest()
                result = workspace.discover_workspace(root)
                self.assertEqual(len(result["entries"]), 1)
                entry = result["entries"][0]
                self.assertEqual(entry["name"], "Saved survey")
                self.assertFalse(entry["complete"])
                self.assertIn("unsupported database structure",
                              entry["detail"])
                with self.assertRaises(ProjectStoreError):
                    workspace.check_load_target(str(store.location))
                self.assertEqual(
                    hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_missing_database_is_not_created_for_a_name(self) -> None:
        """Return no name instead of creating a database at a missing path.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.sqlite3"
            self.assertEqual(workspace._project_name(path), "")
            self.assertFalse(path.exists())
