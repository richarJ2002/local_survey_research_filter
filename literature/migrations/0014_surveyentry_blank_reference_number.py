"""
Allow blank survey reference numbers assigned after adding the entry.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   0014_surveyentry_blank_reference_number.py

@Brief:  Schema-only optionality; existing reference numbers unchanged.

@Date:   15/10/2026
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
    Permit unnumbered curated entries; uniqueness still applies when set.
    """

    dependencies = (("literature", "0013_search_is_current"),)

    operations = (
        migrations.AlterField(
            model_name="surveyentry", name="reference_number",
            field=models.PositiveIntegerField(null=True, blank=True),
        ),
        migrations.RemoveConstraint(
            model_name="surveyentry",
            name="positive_survey_reference_number",
        ),
        migrations.AddConstraint(
            model_name="surveyentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    reference_number__gte=1) | models.Q(
                    reference_number__isnull=True),
                name="positive_survey_reference_number",
            ),
        ),
    )
