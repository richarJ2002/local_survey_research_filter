"""
Export one stored project's research to a human-readable Markdown file.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   export_markdown.py

@Brief:  Export one stored project's research to a human-readable Markdown file.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

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
    """
    Export a project's literature database to Markdown.
    """

    help = "Export PROJECT_ID to a Markdown file generated from SQLite truth."

    def add_arguments(self, parser) -> None:
        """
        Register project ID and optional output path with Django's parser.

        Args:
            parser (ArgumentParser): Django-owned parser to extend.

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Add integer project_id and --output, whose default is empty text.
        """
        parser.add_argument("project_id", type=int)
        parser.add_argument("--output", type=str, default="")

    def handle(self, *args, **options) -> str:
        """
        Write the project's Markdown export and report its destination.

        Args:
            *args: Django's unused positional command arguments.

        Kwargs:
            **options: Parsed command options: project_id selects the stored
                project; output is a path string, defaulting to empty text to
                use PROJECTS_DIR/exports/project-<pk>-literature.md. Other
                Django command options are not read here.

        Returns:
            str: Output path as supplied or constructed, not canonicalized.

        Raises:
            CommandError: If project_id does not name a stored project.
            OSError: If output directory creation or file writing fails.

        Side Effects:
            Create output parents, overwrite UTF-8 Markdown and print Wrote
            plus its path. The export service may ensure default stages when
            not in worker role; existing research rows are rendered, not seeded
            or rewritten by this command.
        """
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
