"""
@File:   export_markdown.py

@Brief:  Management command to export project Markdown database.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

"""

# Regular library imports
from __future__ import annotations

from pathlib import Path

# Custom library Imports
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from literature.models import Project
from literature.services.export_service import write_markdown

# ------------------------------------------------------------------------ #
# Command
# ------------------------------------------------------------------------ #


class Command(BaseCommand):
    """Export a project's literature database to Markdown."""

    help = "Export PROJECT_ID to a Markdown file generated from SQLite truth."

    def add_arguments(self, parser) -> None:
        """Add project id and output args."""
        parser.add_argument("project_id", type=int)
        parser.add_argument("--output", type=str, default="")

    def handle(self, *args, **options) -> str:
        """Run the export."""
        try:
            project = Project.objects.get(pk=options["project_id"])
        except Project.DoesNotExist as exc:
            raise CommandError(f"No project {options['project_id']}") from exc
        output = (
            Path(options["output"])
            if options["output"]
            else settings.PROJECTS_DIR / "exports" / f"project-{project.pk}-literature.md"
        )
        write_markdown(project, output)
        self.stdout.write(f"Wrote {output}")
        return str(output)
