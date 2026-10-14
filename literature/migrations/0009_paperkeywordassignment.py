"""
Store the current keyword assignment without replacing discovery provenance.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   0009_paperkeywordassignment.py

@Brief:  Store the current keyword assignment without replacing discovery provenance.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from typing import ClassVar

# Custom library Imports
import django.db.models.deletion
from django.db import migrations, models

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class Migration(migrations.Migration):
    """
    Create one keyword assignment per paper/project linked to its search.
    """

    dependencies: ClassVar = [("literature", "0008_screeningrevision")]

    operations: ClassVar = [
        migrations.CreateModel(
            name="PaperKeywordAssignment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("keyword", models.CharField(max_length=255)),
                (
                    "paper",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="keyword_assignments",
                        to="literature.paper",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="keyword_assignments",
                        to="literature.project",
                    ),
                ),
                (
                    "search",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="keyword_assignments",
                        to="literature.search",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("paper", "project"), name="unique_project_paper_keyword"
                    )
                ]
            },
        ),
    ]
