#!/usr/bin/env python3

'''
@File:   search.py

@Brief:  Search configuration and per-provider run audit.

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

from literature.models.project import Project
from literature.providers import DEFAULT_SEARCH_PROVIDERS, SEARCH_PROVIDERS

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes
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
