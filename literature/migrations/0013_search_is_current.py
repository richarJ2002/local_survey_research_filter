"""Add a current-search flag without selecting or rewriting legacy settings.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   0013_search_is_current.py

@Brief:  Schema-only singleton; existing configurations remain historical.

@Date:   07/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# @File: 0013_search_is_current.py
# @Brief: Schema-only singleton; existing configurations remain historical.
# @Date: 07/10/2026

# Custom library Imports
from django.db import migrations, models

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class Migration(migrations.Migration):
    """Enforce at most one explicitly adopted current search per project."""

    dependencies = (("literature", "0012_surveyentry_inclusion_origin"),)

    operations = (
        migrations.AddField(
            model_name="search", name="is_current",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="search",
            constraint=models.UniqueConstraint(
                fields=("project",), condition=models.Q(is_current=True),
                name="unique_current_search_per_project",
            ),
        ),
    )
