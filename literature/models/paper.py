#!/usr/bin/env python3

'''
@File:   paper.py

@Brief:  Canonical papers and cached citation edges.

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
