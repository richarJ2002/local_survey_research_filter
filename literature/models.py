#!/usr/bin/env python3

'''
@File:   models.py

@Brief:  Canonical domain models for Local Survey Research Filter (SQLite source of truth).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.core.exceptions import ValidationError
from django.db import models

# ------------------------------------------------------------------------ #
# Project / Search / SearchRun
# ------------------------------------------------------------------------ #


class Project(models.Model):
    """Top-level survey container (topic-agnostic)."""

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    survey_topic = models.TextField(blank=True, default="")
    research_questions = models.TextField(blank=True, default="")
    domain = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name

    def ensure_default_stages(self) -> None:
        """Seed Title (order 1) + Abstract (order 2) stages if missing."""
        defaults = [("Title", 1, False), ("Abstract", 2, True)]
        for name, order, is_final in defaults:
            Stage.objects.get_or_create(
                project=self,
                order=order,
                defaults={"name": name, "is_final": is_final},
            )


class Search(models.Model):
    """A named query configuration inside a project.

    Keywords/venues/authors are stored as sorted atomic lists so different
    providers can build their own queries from the same rows.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="searches"
    )
    name = models.CharField(max_length=255)
    query = models.TextField(
        blank=True, default="", help_text="Derived from keywords; kept for audit."
    )
    keywords = models.JSONField(default=list)
    venues = models.JSONField(default=list)
    author_names = models.JSONField(default=list)
    source = models.CharField(max_length=50, default="openalex")
    sources_selected = models.JSONField(
        default=list,
        help_text="Checked providers remembered per search.",
    )
    year_min = models.PositiveIntegerField(null=True, blank=True)
    year_max = models.PositiveIntegerField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    results_count = models.PositiveIntegerField(default=0)
    new_papers_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.project.name} / {self.name}"

    # --- Normalise one row list ---
    @staticmethod
    def clean_rows(values: list) -> list[str]:
        """Strip, drop empties, dedupe (case-insensitive), sort A-Z."""
        seen: dict[str, str] = {}
        for raw in values or []:
            text = str(raw).strip()
            if not text:
                continue
            key = text.lower()
            if key not in seen:
                seen[key] = text
        return sorted(seen.values(), key=str.lower)

    # --- Query derivation ---
    def build_query(self) -> str:
        """Join sorted keywords with spaces for keyword-search APIs."""
        return " ".join(self.clean_rows(self.keywords))

    def save(self, *args, **kwargs) -> None:
        """Sort rows A-Z, default sources, and rebuild query before saving."""
        self.keywords = self.clean_rows(self.keywords)
        self.venues = self.clean_rows(self.venues)
        self.author_names = self.clean_rows(self.author_names)
        canonical = ["openalex", "semantic_scholar", "crossref", "arxiv"]
        selected = [s for s in canonical if s in (self.sources_selected or [])]
        self.sources_selected = selected or canonical
        derived = self.build_query()
        if derived:
            self.query = derived
        super().save(*args, **kwargs)


class SearchRun(models.Model):
    """Immutable audit record of one provider execution."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    search = models.ForeignKey(
        Search, on_delete=models.CASCADE, related_name="runs"
    )
    executed_at = models.DateTimeField(auto_now_add=True)
    query_used = models.TextField(default="", blank=True)
    source = models.CharField(max_length=50, default="openalex")
    results_count = models.PositiveIntegerField(default=0)
    new_papers_count = models.PositiveIntegerField(default=0)
    duplicates_count = models.PositiveIntegerField(default=0)
    no_abstract_count = models.PositiveIntegerField(
        default=0, help_text="Items with blank abstract at import (legacy metric)."
    )
    incomplete_count = models.PositiveIntegerField(
        default=0,
        help_text="Legacy metadata-gap audit; new runs count only missing titles, not screening rejections.",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-executed_at"]

    def __str__(self) -> str:
        return f"Run {self.pk} {self.source} {self.status}"


class Paper(models.Model):
    """Canonical paper. Domain-independent; no hard-coded research fields."""

    openalex_id = models.CharField(
        max_length=64, null=True, blank=True, db_index=True
    )
    title = models.TextField()
    authors = models.JSONField(default=list)
    abstract = models.TextField(blank=True, default="")
    publication_year = models.PositiveIntegerField(null=True, blank=True)
    publication_date = models.DateField(null=True, blank=True)
    venue = models.CharField(max_length=500, blank=True, default="")
    publisher = models.CharField(max_length=300, blank=True, default="")
    doi = models.CharField(
        max_length=320, null=True, blank=True, db_index=True
    )
    urls = models.JSONField(default=list)
    keywords = models.JSONField(default=list)
    citation_count = models.PositiveIntegerField(default=0)
    references = models.JSONField(default=list)
    reference_text = models.TextField(blank=True, default="", help_text="Manually supplied bibliography, including references without resolvable IDs.")
    source_ids = models.JSONField(default=dict)
    sources = models.JSONField(default=list)
    raw = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["publication_year"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["doi"],
                condition=models.Q(doi__isnull=False),
                name="unique_doi_when_present",
            ),
            models.UniqueConstraint(
                fields=["openalex_id"],
                condition=models.Q(openalex_id__isnull=False),
                name="unique_openalex_when_present",
            ),
        ]

    def __str__(self) -> str:
        return self.title[:120]

    def author_display(self) -> str:
        """Human-readable author list for Round 1 card."""
        names: list[str] = []
        for entry in self.authors or []:
            if isinstance(entry, dict):
                name = str(entry.get("name", "")).strip()
            else:
                name = str(entry).strip()
            if name:
                names.append(name)
        if not names:
            return "Unknown authors"
        if len(names) > 4:
            return ", ".join(names[:3]) + f" et al. ({len(names)} authors)"
        return ", ".join(names)


class OpenCitationsLookup(models.Model):
    """DOI citation edges fetched explicitly, cached across projects."""

    doi = models.CharField(max_length=320)
    direction = models.CharField(max_length=10)
    edges = models.JSONField(default=list)
    checked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["doi", "direction"], name="unique_opencitations_doi_direction"
            )
        ]


class Discovery(models.Model):
    """Provenance: how a paper entered the corpus. Never deleted silently."""

    paper = models.ForeignKey(
        Paper, on_delete=models.CASCADE, related_name="discoveries"
    )
    search = models.ForeignKey(
        Search, on_delete=models.CASCADE, related_name="discoveries"
    )
    search_run = models.ForeignKey(
        SearchRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discoveries",
    )
    source = models.CharField(max_length=50, default="openalex")
    query = models.TextField(default="", blank=True)
    retrieved_at = models.DateTimeField(auto_now_add=True)
    raw_payload = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["paper", "source", "search"],
                name="unique_discovery_per_source_search",
            )
        ]

    def __str__(self) -> str:
        return f"{self.paper_id} via {self.source}"


class Stage(models.Model):
    """Screening stage. MVP seeds Title (1) -> Abstract (2, final)."""

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
        return f"{self.project.name} / {self.name}"


class ScreeningDecision(models.Model):
    """Yes/No decision per paper per stage. Immutable once decided."""

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
        return f"{self.paper_id} @ {self.stage.name}: {self.decision}"

    def clean(self) -> None:
        if self.decision not in {
            self.Decision.PENDING,
            self.Decision.ACCEPT,
            self.Decision.REJECT,
        }:
            raise ValidationError(f"Invalid decision: {self.decision}")


class ScreeningRevision(models.Model):
    """Audit an explicit correction made from the project paper manager."""

    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name="revisions")
    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="revisions")
    previous_decision = models.CharField(max_length=10)
    new_decision = models.CharField(max_length=10)
    reason = models.CharField(max_length=255)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-pk"]


class PaperKeywordAssignment(models.Model):
    """!
    @brief Store the current project-specific keyword separately from discovery history.

    A canonical paper can be shared across projects; changing one project's
    assignment must neither rewrite provider provenance nor affect another project.
    """

    paper = models.ForeignKey(Paper, on_delete=models.CASCADE, related_name="keyword_assignments")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="keyword_assignments")
    search = models.ForeignKey(Search, on_delete=models.CASCADE, related_name="keyword_assignments")
    keyword = models.CharField(max_length=255)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["paper", "project"], name="unique_project_paper_keyword"),
        )


class SurveyEntry(models.Model):
    """Editable survey-writing snapshot; never overwrites the screening library."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="survey_entries")
    paper = models.ForeignKey(Paper, on_delete=models.SET_NULL, null=True, blank=True)
    reference_number = models.PositiveIntegerField()
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
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("reference_number", "pk")
        constraints = (
            models.UniqueConstraint(fields=["project", "reference_number"], name="unique_survey_reference_number"),
            models.CheckConstraint(condition=models.Q(reference_number__gte=1), name="positive_survey_reference_number"),
            models.UniqueConstraint(fields=["project", "doi"], condition=~models.Q(doi=""), name="unique_survey_doi_when_present"),
        )

    def __str__(self) -> str:
        return f"[{self.reference_number}] {self.title}"


class SurveyCitation(models.Model):
    """A curator-confirmed citing → cited link between entries in the same project."""

    citing = models.ForeignKey(SurveyEntry, on_delete=models.CASCADE, related_name="manual_outgoing")
    cited = models.ForeignKey(SurveyEntry, on_delete=models.CASCADE, related_name="manual_incoming")

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["citing", "cited"], name="unique_manual_survey_citation"),
            models.CheckConstraint(condition=~models.Q(citing=models.F("cited")), name="no_survey_self_citation"),
        )
