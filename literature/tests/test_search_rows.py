#!/usr/bin/env python3

'''
@File:   test_search_rows.py

@Brief:  Tests for sorted keyword/venue/author rows and query derivation.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.forms import SearchForm
from literature.models import Project, Search

# ------------------------------------------------------------------------ #
# Row tests
# ------------------------------------------------------------------------ #


class SearchRowTests(TestCase):
    """Rows sort A-Z, dedupe, and build the provider query."""

    # --- Sorting ---
    def test_clean_rows_sorts_and_dedupes(self) -> None:
        """Unsorted/duped rows become sorted unique A-Z."""
        assert Search.clean_rows(["lithium", "Battery", "battery ", "", "  degradation"]) == [
            "Battery",
            "degradation",
            "lithium",
        ]

    # --- Query derivation ---
    def test_save_rebuilds_query(self) -> None:
        """Saving sorts rows and joins keywords into query."""
        project = Project.objects.create(name="P")
        search = Search(project=project, name="T", keywords=["zebra", "apple"])
        search.save()
        assert search.keywords == ["apple", "zebra"]
        assert search.query == "apple zebra"

    # --- Form requires keywords ---
    def test_form_requires_keyword_row(self) -> None:
        """Empty keyword rows are rejected."""
        form = SearchForm(
            data={"name": "T", "keywords_json": "[]", "venues_json": "[]", "author_names_json": "[]"}
        )
        assert not form.is_valid()

    # --- Form sorts rows ---
    def test_form_sorts_rows(self) -> None:
        """Form cleaned rows are sorted A-Z."""
        form = SearchForm(
            data={
                "name": "T",
                "keywords_json": '["lithium", "battery"]',
                "venues_json": '["Nature", "science"]',
                "author_names_json": "[]",
            }
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["keywords"] == ["battery", "lithium"]
