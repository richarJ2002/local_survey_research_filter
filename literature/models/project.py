#!/usr/bin/env python3

'''
@File:   project.py

@Brief:  Project container and screening stages.

@Date:   14/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.db import models

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes
class Project(models.Model):
    """
    Top-level survey container (topic-agnostic).
    """

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    survey_topic = models.TextField(blank=True, default="")
    research_questions = models.TextField(blank=True, default="")
    domain = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        """
        Return the stored project display name.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: The project name, without renaming its folder.
        """
        return self.name

    def ensure_default_stages(self) -> None:
        """
        Create missing Title/Abstract stages during an explicit write.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Create stages at orders 1 and 2; never rewrite existing stages.
        """
        defaults = [("Title", 1, False), ("Abstract", 2, True)]
        for name, order, is_final in defaults:
            Stage.objects.get_or_create(
                project=self,
                order=order,
                defaults={"name": name, "is_final": is_final},
            )


class Stage(models.Model):
    """
    Screening stage. MVP seeds Title (1) -> Abstract (2, final).
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="stages"
    )
    name = models.CharField(max_length=64)
    order = models.PositiveSmallIntegerField()
    is_final = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "order"], name="unique_stage_order"
            )
        ]

    def __str__(self) -> str:
        """
        Identify the screening stage and its owning project.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Project name and stage name separated by a slash.
        """
        return f"{self.project.name} / {self.name}"
