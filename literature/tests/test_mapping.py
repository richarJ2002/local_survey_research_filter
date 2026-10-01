#!/usr/bin/env python3

'''
@File:   test_mapping.py

@Brief:  Tests for OpenAlex mapping and abstract reconstruction.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.providers.mapping import (
    first_author_surname,
    map_openalex_to_canonical,
    normalize_title,
    reconstruct_abstract,
)

# ------------------------------------------------------------------------ #
# Mapping tests
# ------------------------------------------------------------------------ #


class MappingTests(TestCase):
    """Pure mapping helpers must be deterministic."""

    # --- Abstract reconstruction ---
    def test_reconstruct_abstract_orders_words(self) -> None:
        """Inverted index positions must rebuild ordered text."""
        idx = {"Hello": [0], "world": [1]}
        self.assertEqual(reconstruct_abstract(idx), "Hello world")

    # --- Empty abstract ---
    def test_reconstruct_abstract_empty(self) -> None:
        """Missing index returns empty string."""
        self.assertEqual(reconstruct_abstract(None), "")
        self.assertEqual(reconstruct_abstract({}), "")

    # --- Title normalization ---
    def test_normalize_title_strips_punctuation(self) -> None:
        """Titles compare case/punctuation-insensitively."""
        self.assertEqual(normalize_title("Hello,  World!"), "hello world")

    # --- First author surname ---
    def test_first_author_surname_takes_last_token(self) -> None:
        """Surname is last whitespace token, lowercased."""
        self.assertEqual(first_author_surname([{"name": "Jane A. Doe"}]), "doe")
        self.assertEqual(first_author_surname([]), "")

    # --- Full canonical mapping ---
    def test_map_openalex_to_canonical_minimal(self) -> None:
        """Minimal OpenAlex item maps to canonical dict."""
        item = {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1234/ABC",
            "title": "Test Title",
            "publication_year": 2024,
            "publication_date": "2024-03-15",
            "authorships": [
                {
                    "author": {"display_name": "Jane Doe", "orcid": None},
                    "institutions": [{"display_name": "Uni"}],
                }
            ],
            "primary_location": {
                "source": {"display_name": "Test J", "publisher": "Pub"},
                "pdf_url": None,
                "landing_page_url": "https://example.com",
            },
            "locations": [],
            "abstract_inverted_index": {"A": [0], "test": [1]},
            "cited_by_count": 5,
            "referenced_works": [],
            "keywords": [{"display_name": "k1"}],
        }
        paper_data, disc = map_openalex_to_canonical(item)
        self.assertEqual(paper_data["openalex_id"], "W123")
        self.assertEqual(paper_data["doi"], "10.1234/abc")
        self.assertEqual(paper_data["abstract"], "A test")
        self.assertEqual(paper_data["venue"], "Test J")
        self.assertEqual(disc["source"], "openalex")
