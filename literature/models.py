#!/usr/bin/env python3

'''
@File:   models.py

@Brief:  Canonical domain models for Local Survey Research Filter (SQLite source of truth).

@Date:   28/09/2026
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

from literature.providers import DEFAULT_SEARCH_PROVIDERS, SEARCH_PROVIDERS

# ------------------------------------------------------------------------ #
# Project / Search / SearchRun
# ------------------------------------------------------------------------ #


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


class Search(models.Model):
    """
    A named query configuration inside a project.

    Keywords/venues/authors are stored as sorted atomic lists so different
    providers can build their own queries from the same rows.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="searches"
    )
    is_current = models.BooleanField(default=False)
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

    class Meta:
        """
        Allow history and zero current settings, but never two current rows.
        """

        constraints = (
            models.UniqueConstraint(
                fields=["project"], condition=models.Q(is_current=True),
                name="unique_current_search_per_project",
            ),
        )

    def __str__(self) -> str:
        """
        Identify a configuration within its owning project.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Project name and configuration name separated by a slash.
        """
        return f"{self.project.name} / {self.name}"

    # --- Normalise one row list ---
    @staticmethod
    def clean_rows(values: list) -> list[str]:
        """
        Normalize rows while retaining the first spelling of each value.

        Args:
            values: Values to stringify and strip; empty/None yields no rows.

        Kwargs:
            None

        Returns:
            list[str]: Nonblank, lower-case-key-deduplicated, A–Z sorted rows.
        """
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
        """
        Derive a space-joined query from normalized keyword rows.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Sorted keyword text, or blank if no keyword rows exist.
        """
        return " ".join(self.clean_rows(self.keywords))

    def save(self, *args, **kwargs) -> None:
        """
        Normalize future settings before delegating persistence to Django.

        Args:
            *args: Positional Model.save arguments forwarded unchanged.

        Kwargs:
            **kwargs: Model.save options such as using, force_insert,
                force_update and update_fields; Django defaults apply.

        Returns:
            None

        Raises:
            IntegrityError: If persistence violates a database constraint.

        Side Effects:
            Normalize row lists and effective provider choices on this object;
            derive a nonblank keyword query, retaining query-only fallback.
            Empty selections retain the original four defaults; Scopus is
            opt-in. update_fields controls which fields are persisted.
        """
        self.keywords = self.clean_rows(self.keywords)
        self.venues = self.clean_rows(self.venues)
        self.author_names = self.clean_rows(self.author_names)
        selected = [source for source in SEARCH_PROVIDERS
                    if source in (self.sources_selected or [])]
        self.sources_selected = selected or list(DEFAULT_SEARCH_PROVIDERS)
        derived = self.build_query()
        if derived:
            self.query = derived
        super().save(*args, **kwargs)


class SearchRun(models.Model):
    """
    Immutable audit record of one provider execution.
    """

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
        """
        Identify a provider run and its current audit status.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Run ID, provider identifier and status.
        """
        return f"Run {self.pk} {self.source} {self.status}"


class Paper(models.Model):
    """
    Canonical paper. Domain-independent; no hard-coded research fields.
    """

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
    reference_text = models.TextField(
        blank=True, default="", help_text="Manually supplied bibliography, including references without resolvable IDs.")
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
        """
        Provide a bounded paper title for administrative displays.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: The first 120 title characters.
        """
        return self.title[:120]

    def author_display(self) -> str:
        """
        Format author names without exposing raw provider author objects.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Comma-joined names up to four authors; first three plus
            et al. and total count for longer lists, or Unknown authors.
        """
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
    """
    DOI citation edges fetched explicitly, cached across projects.
    """

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
    """
    Provenance: how a paper entered the corpus. Never deleted silently.
    """

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
        """
        Describe a recorded discovery without changing its provenance.

        Args:
            None

        Kwargs:
            None

        Returns:
            str: Paper ID and discovery source.
        """
        return f"{self.paper_id} via {self.source}"


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
                reference_number__gte=1), name="positive_survey_reference_number"),
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
            str: Bracketed reference number and complete curated title.
        """
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
