#!/usr/bin/env python3

'''
@File:   discovery.py

@Brief:  Corpus provenance: how papers entered.

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
from literature.models.search import Search, SearchRun

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes
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
