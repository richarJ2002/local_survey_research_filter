#!/usr/bin/env python3

"""
Verify Title admission despite missing later-stage metadata.

@File:   test_no_abstract.py

@Brief:  Tests for initial filter: dedup, enrichment, incomplete gate.

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

from literature.models import Paper, Project, ScreeningDecision, Search
from literature.models import SearchRun
from literature.services.search_service import (
    _store_batch, gate_papers, is_complete,
)

# ------------------------------------------------------------------------ #
# Filter tests
# ------------------------------------------------------------------------ #


class InitialFilterTests(TestCase):
    """
    Store everything, enrich, then gate complete papers into screening.
    """

    # --- Helpers ---
    def _setup(self):
        """
        Create the search and Title-stage fixtures for batch admission.

        Project, search, run, title stage.

        Args:
            None

        Kwargs:
            None

        Returns:
            tuple: Project, search, running OpenAlex audit, and Title stage.
        """
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(
            project=project, name="S", keywords=["a"])
        run = SearchRun.objects.create(
            search=search, source="openalex", status="running")
        return project, search, run, project.stages.get(name="Title")

    class Item:
        """
        Supply canonical paper data and an empty raw provider response.
        """

        def __init__(self, data: dict) -> None:
            """
            Wrap canonical metadata as a provider item for batch storage.

            Args:
                data (dict): Canonical paper fields consumed by _store_batch.

            Kwargs:
                None

            Returns:
                None
            """
            self.paper_data = data
            self.raw_response = {}

    def _data(self, **overrides):
        """
        Build canonical metadata with an abstract, year, and references.

        Complete paper payload.

        Args:
            None

        Kwargs:
            overrides (dict): Collected canonical field replacements such as
                doi, abstract, or references applied to the complete base
                payload.

        Returns:
            dict: Fresh canonical paper payload including the supplied changes.
        """
        base = {
            "title": "T", "authors": [], "abstract": "Some text",
            "publication_year": 2020, "publication_date": None, "venue": "",
            "publisher": "", "doi": None, "urls": [], "keywords": [],
            "citation_count": 0, "references": ["https://openalex.org/W1"],
            "source_ids": {}, "sources": ["openalex"], "raw": {},
            "openalex_id": None,
        }
        base.update(overrides)
        return base

    # --- Complete paper enters queue ---
    def test_complete_paper_gated_in(self) -> None:
        """
        Create a Title decision for a stored complete canonical paper.

        Stored paper with all fields gets a Title decision.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        _project, search, run, title_stage = self._setup()
        _new, _dups, _no_abs, touched = _store_batch(
            search=search, source_key="openalex", run=run,
            items=[self.Item(self._data(doi="10.1/a"))],
            title_stage=title_stage, query_for_provenance="a",
            create_decisions=False,
        )
        papers = list(Paper.objects.filter(pk__in=touched))
        incomplete = gate_papers(papers, title_stage)
        self.assertEqual(incomplete, 0)
        self.assertTrue(
            ScreeningDecision.objects.filter(
                paper=papers[0], stage=title_stage
            ).exists()
        )

    # --- Incomplete paper stays out ---
    def test_missing_abstract_and_references_still_enter_title(self) -> None:
        """
        Admit a titled paper despite blank abstract text and no references.

        Missing later-stage metadata must not exclude a titled paper.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        _project, search, run, title_stage = self._setup()
        _new, _dups, _no_abs, touched = _store_batch(
            search=search, source_key="openalex", run=run,
            items=[self.Item(self._data(
                doi="10.1/b", abstract="  ", references=[]))],
            title_stage=title_stage, query_for_provenance="a",
            create_decisions=False,
        )
        papers = list(Paper.objects.filter(pk__in=touched))
        self.assertFalse(is_complete(papers[0]))
        incomplete = gate_papers(papers, title_stage)
        self.assertEqual(incomplete, 0)
        self.assertTrue(
            ScreeningDecision.objects.filter(
                paper=papers[0], stage=title_stage
            ).exists()
        )

    # --- Enrichment completes ---
    def test_enrichment_completes_then_gates(self) -> None:
        """
        Verify Title admission both before and after simulated enrichment.

        OpenAlex lookup filling blanks lets the paper through.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        _project, search, run, title_stage = self._setup()
        _new, _dups, _no_abs, touched = _store_batch(
            search=search, source_key="crossref", run=run,
            items=[self.Item(self._data(
                doi="10.1/c", abstract="", references=[]))],
            title_stage=title_stage, query_for_provenance="a",
            create_decisions=False,
        )
        papers = list(Paper.objects.filter(pk__in=touched))
        self.assertEqual(gate_papers(papers, title_stage), 0)
        # Simulate enrichment filling the blanks (as enrich_papers would).
        paper = papers[0]
        paper.abstract = "Now filled"
        paper.references = ["https://openalex.org/W9"]
        paper.save(update_fields=["abstract", "references"])
        self.assertEqual(gate_papers([paper], title_stage), 0)
        self.assertTrue(
            ScreeningDecision.objects.filter(
                paper=paper, stage=title_stage
            ).exists()
        )
