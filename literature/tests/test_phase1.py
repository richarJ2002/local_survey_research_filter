#!/usr/bin/env python3

'''
@File:   test_phase1.py

@Brief:  Tests for project context fields and remembered source ticks.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.forms import ProjectForm, SearchForm
from literature.models import Project, Search

# ------------------------------------------------------------------------ #
# Phase 1 tests
# ------------------------------------------------------------------------ #


class Phase1Tests(TestCase):
    """Project context + source ticks remembered per search."""

    # --- Project fields ---
    def test_project_context_fields(self) -> None:
        """Topic/RQs/domain save and render."""
        form = ProjectForm(
            data={
                "name": "P",
                "survey_topic": "Battery",
                "research_questions": "RQ1?",
                "domain": "energy",
                "description": "",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        project = form.save()
        self.assertEqual(project.survey_topic, "Battery")
        self.assertEqual(project.domain, "energy")

    # --- Sources remembered ---
    def test_sources_default_all_and_remembered(self) -> None:
        """Blank ticks default to all four in canonical order."""
        project = Project.objects.create(name="P")
        search = Search(project=project, name="S", keywords=["a"])
        search.save()
        self.assertEqual(
            search.sources_selected,
            ["openalex", "semantic_scholar", "crossref", "arxiv"],
        )

    # --- Form ticks ---
    def test_search_form_ticks(self) -> None:
        """Ticked sources survive validation."""
        form = SearchForm(
            data={
                "name": "S",
                "keywords_json": '["a"]',
                "venues_json": "[]",
                "author_names_json": "[]",
                "sources_selected": ["openalex", "arxiv"],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["sources_selected"], ["openalex", "arxiv"])

    # --- Edit reconfigures ---
    def test_search_update_reconfigures(self) -> None:
        """Edit view updates rows without requiring a new search."""
        project = Project.objects.create(name="P")
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        url = f"/project/{project.pk}/search/{search.pk}/edit/"
        get_resp = self.client.get(url)
        self.assertEqual(get_resp.status_code, 200)
        post_resp = self.client.post(
            url,
            data={
                "name": "S2",
                "keywords_json": '["b", "a"]',
                "venues_json": "[]",
                "author_names_json": "[]",
                "sources_selected": ["openalex"],
            },
        )
        self.assertEqual(post_resp.status_code, 302)
        search.refresh_from_db()
        self.assertEqual(search.name, "S2")
        self.assertEqual(search.keywords, ["a", "b"])
        self.assertEqual(search.sources_selected, ["openalex"])
