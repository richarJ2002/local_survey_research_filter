#!/usr/bin/env python3

'''
@File:   test_trustworthy_sources.py

@Brief:  Tests for Crossref/arXiv mapping and metrics/duplicates.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.models import Paper, Project, Search, SearchRun
from literature.providers.arxiv import ArxivSource
from literature.providers.crossref import CrossrefSource
from literature.providers.mapping import (
    map_arxiv_to_canonical,
    map_crossref_to_canonical,
)
from literature.services.dedup_service import duplicate_groups, merge_papers
from literature.services.metrics_service import build_metrics

# ------------------------------------------------------------------------ #
# Provider tests
# ------------------------------------------------------------------------ #


class TrustworthyMappingTests(TestCase):
    """Crossref/arXiv map to canonical shape without keys."""

    # --- Crossref ---
    def test_crossref_minimal(self) -> None:
        """Crossref DOI/title/venue map; abstract empty."""
        item = {
            "DOI": "10.1234/ABC",
            "title": ["Crossref Title"],
            "author": [{"given": "Jane", "family": "Doe"}],
            "container-title": ["Nature"],
            "publisher": "Springer",
            "URL": "https://doi.org/10.1234/abc",
            "is-referenced-by-count": 3,
            "subject": ["Biology"],
            "published-print": {"date-parts": [[2022, 1, 5]]},
        }
        paper_data, disc = map_crossref_to_canonical(item)
        self.assertEqual(paper_data["doi"], "10.1234/abc")
        self.assertEqual(paper_data["venue"], "Nature")
        self.assertEqual(paper_data["sources"], ["crossref"])
        self.assertEqual(disc["source"], "crossref")

    # --- arXiv ---
    def test_arxiv_minimal(self) -> None:
        """arXiv Atom entry maps abstract + arxiv id."""
        item = {
            "id": "http://arxiv.org/abs/2301.00001v1",
            "title": "Arxiv Title",
            "summary": "Summary here.",
            "published": "2023-01-02T00:00:00Z",
            "authors": ["Jane Doe"],
            "doi": "",
            "journal_ref": "",
            "primary_category": "cs.AI",
            "categories": ["cs.AI"],
        }
        paper_data, disc = map_arxiv_to_canonical(item)
        self.assertEqual(paper_data["abstract"], "Summary here.")
        self.assertEqual(paper_data["sources"], ["arxiv"])
        self.assertEqual(disc["source"], "arxiv")

    # --- arXiv query ---
    def test_arxiv_query_joins_keywords(self) -> None:
        """Keyword rows become OR-joined all: query."""
        from literature.providers.arxiv import ArxivConfig

        src = ArxivSource()
        cfg = ArxivConfig(query="battery lithium", keywords=("battery", "lithium"))
        self.assertIn('all:"battery"', src.build_search_query(cfg))

    # --- Crossref headers carry mailto ---
    def test_crossref_headers_polite(self) -> None:
        """Crossref UA includes mailto when configured."""
        src = CrossrefSource(email="a@b.c")
        self.assertIn("mailto:a@b.c", src.default_headers()["User-Agent"])


class MetricsDuplicateTests(TestCase):
    """Metrics funnel + same-title duplicate review."""

    # --- Metrics ---
    def test_metrics_groups_by_source(self) -> None:
        """Per-source totals, dups, unique, and stage counts."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        SearchRun.objects.create(
            search=search, source="openalex", status="completed",
            results_count=10, new_papers_count=7, duplicates_count=3,
        )
        SearchRun.objects.create(
            search=search, source="crossref", status="completed",
            results_count=4, new_papers_count=4, duplicates_count=0,
        )
        metrics = build_metrics(project)
        self.assertEqual(metrics["total_identified"], 14)
        self.assertEqual(metrics["per_source"]["openalex"]["results"], 10)
        self.assertEqual(metrics["unique_total"], 0)

    # --- Duplicates ---
    def test_duplicate_groups_and_merge(self) -> None:
        """Same titles group; merge keeps history, drops duplicate."""
        project = Project.objects.create(name="P")
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        from literature.models import Discovery

        keep = Paper.objects.create(title="Same Title", authors=[{"name": "A B"}])
        drop = Paper.objects.create(title="same title!", authors=[{"name": "A B"}])
        Discovery.objects.create(paper=keep, search=search, source="openalex", query="a")
        Discovery.objects.create(paper=drop, search=search, source="crossref", query="a")
        groups = duplicate_groups(project)
        self.assertEqual(len(groups), 1)
        merge_papers(keep=keep, drop=drop)
        self.assertEqual(Paper.objects.count(), 1)
        self.assertTrue(keep.discoveries.count() >= 2)
