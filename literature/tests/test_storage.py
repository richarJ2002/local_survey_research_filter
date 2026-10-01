"""Private storage defaults and explicit export overrides.

@File: test_storage.py
@Brief: Keep default generated files under ignored projects/.
@Date: 02/10/2026
"""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from literature.models import Project


# --- No real output files or survey data ---
class StorageTests(TestCase):
    def test_export_defaults_and_explicit_override(self):
        project = Project.objects.create(name="Export test")
        for output, expected in (
            ("", settings.PROJECTS_DIR / "exports" / f"project-{project.pk}-literature.md"),
            ("/chosen/lit.md", Path("/chosen/lit.md")),
        ):
            with patch("literature.management.commands.export_markdown.write_markdown") as write:
                call_command("export_markdown", project.pk, output=output, stdout=StringIO())
            self.assertEqual(write.call_args.args, (project, expected))
        self.assertEqual(settings.PROJECTS_DIR, settings.BASE_DIR / "projects")
