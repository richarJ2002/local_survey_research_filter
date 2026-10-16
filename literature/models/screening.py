#!/usr/bin/env python3

'''
@File:   screening.py

@Brief:  Stage decisions and correction audit.

@Date:   14/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.core.exceptions import ValidationError
from django.db import models

from literature.models.paper import Paper
from literature.models.project import Stage

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes
class ScreeningDecision(models.Model):
    """
    Yes/No decision per paper per stage. Immutable once decided.
    """

    class Decision(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPT = "ACCEPT", "Accept"
        REJECT = "REJECT", "Reject"
        # MAYBE deferred: add ("MAYBE", "Maybe") later with zero data migration.

    paper = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="decisions"
    )
    stage = models.ForeignKey(
        Stage, on_delete=models.CASCADE, related_name="decisions"
    )
    decision = models.CharField(
        max_length=10, choices=Decision.choices, default=Decision.PENDING
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["paper", "stage"], name="unique_decision_per_stage"
            )
        ]
        indexes = [
            models.Index(fields=["stage", "decision"]),
        ]

    def __str__(self) -> str:
        """
        Describe the stored paper/stage decision for an audit display.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Paper ID, stage name and decision value.
        """
        return f"{self.paper_id} @ {self.stage.name}: {self.decision}"

    def clean(self) -> None:
        """
        Validate the finite decision vocabulary without saving a choice.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Raises:
            ValidationError: Unless the value is PENDING, ACCEPT or REJECT.
        """
        if self.decision not in {
            self.Decision.PENDING,
            self.Decision.ACCEPT,
            self.Decision.REJECT,
        }:
            raise ValidationError(f"Invalid decision: {self.decision}")


class ScreeningRevision(models.Model):
    """
    Audit an explicit correction made from the project paper manager.
    """

    paper = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="revisions")
    stage = models.ForeignKey(
        Stage, on_delete=models.CASCADE, related_name="revisions")
    previous_decision = models.CharField(max_length=10)
    new_decision = models.CharField(max_length=10)
    reason = models.CharField(max_length=255)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-pk"]
