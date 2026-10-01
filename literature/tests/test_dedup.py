#!/usr/bin/env python3

'''
@File:   test_dedup.py

@Brief:  Tests for DOI and title+author+year deduplication.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.models import Paper
from literature.services.dedup_service import find_duplicate

# ------------------------------------------------------------------------ #
# Dedup tests
# ------------------------------------------------------------------------ #


class DedupTests(TestCase):
    """Dedup must match DOI first, then same normalized title."""

    # --- DOI match ---
    def test_doi_match_wins(self) -> None:
        """Same DOI returns existing paper."""
        existing = Paper.objects.create(title="A", doi="10.1/xyz")
        found, kind = find_duplicate({"doi": "https://doi.org/10.1/XYZ", "title": "Other"})
        assert found is not None and found.pk == existing.pk
        assert kind == "doi"

    # --- Title match ---
    def test_title_match(self) -> None:
        """Same normalized title matches whatever the author/year."""
        existing = Paper.objects.create(
            title="Hello, World!",
            authors=[{"name": "Jane Doe"}],
            publication_year=2020,
        )
        found, kind = find_duplicate(
            {
                "title": "hello world",
                "authors": [{"name": "Someone Else"}],
                "publication_year": 2021,
            }
        )
        assert found is not None and found.pk == existing.pk
        assert kind == "title"

    # --- No match ---
    def test_no_match_returns_none(self) -> None:
        """Different papers return none."""
        Paper.objects.create(title="A", authors=[{"name": "A B"}], publication_year=2020)
        found, kind = find_duplicate(
            {"title": "Totally different", "authors": [{"name": "C D"}], "publication_year": 2021}
        )
        assert found is None
        assert kind == "none"
