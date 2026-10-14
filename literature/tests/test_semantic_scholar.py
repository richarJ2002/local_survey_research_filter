#!/usr/bin/env python3

'''
@File:   test_semantic_scholar.py

@Brief:  Tests for S2 mapping and multi-provider search wiring.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

from unittest.mock import patch

# Custom library Imports
from django.test import TestCase

from literature.models import Paper, Project, Search
from literature.providers.mapping import map_semantic_scholar_to_canonical
from literature.providers.semantic_scholar import (
    SemanticScholarConfig,
    SemanticScholarSource,
)
from literature.services.search_service import run_search

# ------------------------------------------------------------------------ #
# S2 tests
# ------------------------------------------------------------------------ #


class SemanticScholarMappingTests(TestCase):
    """S2 mapping, year handling, and header key behavior."""

    # --- Mapping ---
    def test_map_s2_minimal(self) -> None:
        """Minimal S2 item maps to canonical dict with S2 source."""
        item = {
            "paperId": "abc123",
            "externalIds": {"DOI": "10.1234/XYZ"},
            "title": "S2 Title",
            "abstract": "An abstract.",
            "year": 2023,
            "publicationDate": "2023-05-01",
            "venue": "Nature",
            "authors": [{"name": "Jane Doe"}],
            "citationCount": 7,
            "url": "https://example.com",
            "openAccessPdf": {"url": "https://example.com/p.pdf"},
            "fieldsOfStudy": ["Biology"],
        }
        paper_data, disc = map_semantic_scholar_to_canonical(item)
        self.assertEqual(paper_data["doi"], "10.1234/xyz")
        self.assertEqual(paper_data["sources"], ["semantic_scholar"])
        self.assertEqual(paper_data["source_ids"], {
                         "semantic_scholar": "abc123"})
        self.assertEqual(disc["source"], "semantic_scholar")

    # --- Year param ---
    def test_s2_year_param_max_range(self) -> None:
        """Blank years mean no year filter (max range)."""
        src = SemanticScholarSource(api_key="test-key")
        cfg = SemanticScholarConfig(query="battery")
        self.assertIsNone(src._year_param(cfg))
        cfg2 = SemanticScholarConfig(
            query="battery", year_min=2015, year_max=2026)
        self.assertEqual(src._year_param(cfg2), "2015-2026")

    # --- Headers carry key ---
    def test_s2_headers_carry_key(self) -> None:
        """API key is sent as x-api-key, never in URL."""
        src = SemanticScholarSource(api_key="s2-test")
        self.assertEqual(src.default_headers()["x-api-key"], "s2-test")
        self.assertNotIn(
            "x-api-key", SemanticScholarSource(api_key="").default_headers())


class MultiProviderTests(TestCase):
    """run_search queries both providers and dedupes across them."""

    # --- Both providers stored ---
    def test_run_search_combines_providers(self) -> None:
        """Merge provider duplicates through current settings with isolation.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.providers.openalex import ProviderResult as OAResult
        from literature.providers.semantic_scholar import (
            ProviderResult as S2Result,
        )

        project = Project.objects.create(name="P")
        search = Search.objects.create(
            project=project, name="T", keywords=["battery"], query="battery",
            is_current=True,
        )
        oa_data = {
            "openalex_id": "W1",
            "title": "Shared Title",
            "authors": [{"name": "Jane Doe"}],
            "abstract": "A",
            "publication_year": 2020,
            "publication_date": None,
            "venue": "J",
            "publisher": "",
            "doi": "10.1/shared",
            "urls": [],
            "keywords": [],
            "citation_count": 1,
            "references": [],
            "source_ids": {"openalex": "W1"},
            "sources": ["openalex"],
            "raw": {},
        }
        s2_data = dict(oa_data)
        s2_data["openalex_id"] = None
        s2_data["source_ids"] = {"semantic_scholar": "abc"}
        s2_data["sources"] = ["semantic_scholar"]

        with (
            patch(
                "literature.services.search_service.OpenAlexSource.fetch",
                return_value=(
                    [OAResult(paper_data=oa_data, raw_response={})], 1),
            ),
            patch(
                "literature.services.search_service."
                "SemanticScholarSource.fetch",
                return_value=(
                    [S2Result(paper_data=s2_data, raw_response={})], 1),
            ),
            patch(
                "literature.services.search_service.CrossrefSource.fetch",
                return_value=([], 0),
            ),
            patch(
                "literature.services.search_service.ArxivSource.fetch",
                return_value=([], 0),
            ),
            patch(
                "literature.services.search_service.enrich_papers",
                return_value=0,
            ),
        ):
            runs = run_search(search.pk, max_pages=1)

        assert [r.source for r in runs] == [
            "openalex",
            "semantic_scholar",
            "crossref",
            "arxiv",
        ]
        assert Paper.objects.count() == 1
        paper = Paper.objects.get()
        assert paper.discoveries.count() == 2
