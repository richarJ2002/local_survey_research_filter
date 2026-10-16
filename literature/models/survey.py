#!/usr/bin/env python3

'''
@File:   survey.py

@Brief:  Curated survey snapshots and manual links.

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

from literature.models.paper import Paper
from literature.models.project import Project
from literature.models.search import Search

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class PaperKeywordAssignment(models.Model):
    """
    !
    @brief Store the current project-specific keyword separately from discovery history.

    A canonical paper can be shared across projects; changing one project's
    assignment must neither rewrite provider provenance nor affect another project.
    """

    paper = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="keyword_assignments")
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="keyword_assignments")
    search = models.ForeignKey(
        Search, on_delete=models.CASCADE, related_name="keyword_assignments")
    keyword = models.CharField(max_length=255)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["paper", "project"], name="unique_project_paper_keyword"),
        )


class SurveyEntry(models.Model):
    """
    Editable survey-writing snapshot; never overwrites the screening library.
    """

    class InclusionOrigin(models.TextChoices):
        SEARCH = "search", "Original project search"
        READING = "reading", "Added while reading papers"
        UNSPECIFIED = "unspecified", "Unspecified (legacy)"

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="survey_entries")
    paper = models.ForeignKey(
        Paper, on_delete=models.SET_NULL, null=True, blank=True)
    reference_number = models.PositiveIntegerField(null=True, blank=True)
    citation_count = models.PositiveIntegerField(default=0)
    title = models.TextField()
    doi = models.CharField(max_length=320, blank=True, default="")
    authors = models.TextField(blank=True, default="")
    publication_year = models.PositiveIntegerField(null=True, blank=True)
    venue = models.CharField(max_length=500, blank=True, default="")
    publisher = models.CharField(max_length=300, blank=True, default="")
    abstract = models.TextField(blank=True, default="")
    reference_text = models.TextField(blank=True, default="")
    reference_ids = models.JSONField(default=list)
    identity_aliases = models.JSONField(default=list)
    origin_source = models.CharField(max_length=50)
    inclusion_origin = models.CharField(
        max_length=20,
        choices=InclusionOrigin.choices,
        default=InclusionOrigin.UNSPECIFIED,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("reference_number", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=["project", "reference_number"], name="unique_survey_reference_number"),
            models.CheckConstraint(condition=models.Q(
                reference_number__gte=1) | models.Q(
                reference_number__isnull=True), name="positive_survey_reference_number"),
            models.UniqueConstraint(fields=["project", "doi"], condition=~models.Q(
                doi=""), name="unique_survey_doi_when_present"),
        )

    def __str__(self) -> str:
        """
        Label the independent curated entry with its manuscript reference.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Bracketed reference number and complete curated title,
                or a blank marker when the number is not yet assigned.
        """
        if self.reference_number is None:
            return f"[—] {self.title}"
        return f"[{self.reference_number}] {self.title}"


class SurveyCitation(models.Model):
    """
    A curator-confirmed citing → cited link between entries in the same project.
    """

    citing = models.ForeignKey(
        SurveyEntry, on_delete=models.CASCADE, related_name="manual_outgoing")
    cited = models.ForeignKey(
        SurveyEntry, on_delete=models.CASCADE, related_name="manual_incoming")

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["citing", "cited"], name="unique_manual_survey_citation"),
            models.CheckConstraint(condition=~models.Q(
                citing=models.F("cited")), name="no_survey_self_citation"),
        )
