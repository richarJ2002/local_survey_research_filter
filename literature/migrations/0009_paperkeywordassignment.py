"""Store the current keyword assignment without replacing discovery provenance."""

from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
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
