"""
Snapshot citation counts on curated entries for proportional graph nodes.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   0015_surveyentry_citation_count.py

@Brief:  Schema-only snapshot; existing entries keep zero until re-saved.

@Date:   16/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Custom library Imports
from django.db import migrations, models

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class Migration(migrations.Migration):
    """
    Add a curated citation snapshot without touching existing rows.
    """

    dependencies = (("literature", "0014_surveyentry_blank_reference_number"),)

    operations = (
        migrations.AddField(
            model_name="surveyentry", name="citation_count",
            field=models.PositiveIntegerField(default=0),
        ),
    )
