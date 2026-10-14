#!/usr/bin/env python3

"""Verify search-row normalization, query derivation, and form validation.

@File:   test_search_rows.py

@Brief:  Tests for sorted keyword/venue/author rows and query derivation.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems
    (AERO-Sys) group.

"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

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
        """Trim, drop blanks, dedupe case-insensitively, and sort search rows.

        Unsorted/duped rows become sorted unique A-Z.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        assert Search.clean_rows([
            "lithium", "Battery", "battery ", "", "  degradation",
        ]) == [
            "Battery",
            "degradation",
            "lithium",
        ]

    # --- Query derivation ---
    def test_save_rebuilds_query(self) -> None:
        """Sort saved keywords and derive their space-joined provider query.

        Saving sorts rows and joins keywords into query.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="P")
        search = Search(project=project, name="T", keywords=["zebra", "apple"])
        search.save()
        assert search.keywords == ["apple", "zebra"]
        assert search.query == "apple zebra"

    # --- Form requires keywords ---
    def test_form_requires_keyword_row(self) -> None:
        """Reject a search form with an empty keyword-row list.

        Empty keyword rows are rejected.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        form = SearchForm(
            data={"name": "T", "keywords_json": "[]",
                  "venues_json": "[]", "author_names_json": "[]"}
        )
        assert not form.is_valid()

    # --- Form sorts rows ---
    def test_form_sorts_rows(self) -> None:
        """Sort keyword rows during successful search-form validation.

        Form cleaned rows are sorted A-Z.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
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
