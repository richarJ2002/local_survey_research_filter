"""
Add bibliography text and admit titled papers without replacing decisions.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   0010_reference_text_and_screening_admission.py

@Brief:  Add bibliography text and admit titled papers without replacing decisions.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from typing import ClassVar

# Custom library Imports
from django.db import migrations, models

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Functions


def admit_titled_papers(apps, schema_editor):
    """
    Create missing pending queue rows using historical migration models.

    Args:
        apps (Apps): Migration-state registry for Discovery, Stage and
            ScreeningDecision, not the live model module.
        schema_editor (BaseDatabaseSchemaEditor): Editor whose connection alias
            explicitly selects the migration database for every ORM operation.

    Kwargs:
        None

    Returns:
        None

    Side Effects:
        Ensure stages at orders one/two for discovered projects. Nonblank-title
        papers get a missing Title PENDING row; Title accepts get a missing
        Abstract PENDING row. Existing rows, including accepts/rejects, are not
        changed. Reverse uses RunPython.noop so it does not delete decisions.
    """
    Discovery = apps.get_model("literature", "Discovery")
    Stage = apps.get_model("literature", "Stage")
    Decision = apps.get_model("literature", "ScreeningDecision")
    alias = schema_editor.connection.alias
    for project_id in (
        Discovery.objects.using(alias).values_list(
            "search__project_id", flat=True).distinct()
    ):
        title, _ = Stage.objects.using(alias).get_or_create(
            project_id=project_id, order=1, defaults={"name": "Title", "is_final": False}
        )
        abstract, _ = Stage.objects.using(alias).get_or_create(
            project_id=project_id, order=2, defaults={"name": "Abstract", "is_final": True}
        )
        papers = (
            Discovery.objects.using(alias)
            .filter(search__project_id=project_id)
            .values_list("paper_id", "paper__title")
            .distinct()
        )
        for paper_id, paper_title in papers:
            if not (paper_title or "").strip():
                continue
            current, _ = Decision.objects.using(alias).get_or_create(
                paper_id=paper_id, stage=title, defaults={
                    "decision": "PENDING"}
            )
            if current.decision == "ACCEPT":
                Decision.objects.using(alias).get_or_create(
                    paper_id=paper_id, stage=abstract, defaults={
                        "decision": "PENDING"}
                )


# --- Classes
class Migration(migrations.Migration):
    """
    Add reference_text, clarify counts and admit titled discoveries.
    """

    dependencies: ClassVar = [("literature", "0009_paperkeywordassignment")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="paper",
            name="reference_text",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Manually supplied bibliography, including references without resolvable IDs.",
            ),
        ),
        migrations.AlterField(
            model_name="searchrun",
            name="incomplete_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Legacy metadata-gap audit; new runs count only missing titles, not screening rejections.",
            ),
        ),
        # Reversing the schema must not delete decisions made after admission.
        migrations.RunPython(admit_titled_papers, migrations.RunPython.noop),
    ]
