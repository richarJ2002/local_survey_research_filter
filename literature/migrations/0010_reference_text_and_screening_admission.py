"""Admit previously excluded titled papers without changing existing decisions."""

from typing import ClassVar

from django.db import migrations, models


def admit_titled_papers(apps, schema_editor):
    """Create only missing pending queue rows; preserve accepted/rejected decisions."""
    Discovery = apps.get_model("literature", "Discovery")
    Stage = apps.get_model("literature", "Stage")
    Decision = apps.get_model("literature", "ScreeningDecision")
    alias = schema_editor.connection.alias
    for project_id in (
        Discovery.objects.using(alias).values_list("search__project_id", flat=True).distinct()
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
                paper_id=paper_id, stage=title, defaults={"decision": "PENDING"}
            )
            if current.decision == "ACCEPT":
                Decision.objects.using(alias).get_or_create(
                    paper_id=paper_id, stage=abstract, defaults={"decision": "PENDING"}
                )


class Migration(migrations.Migration):
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
