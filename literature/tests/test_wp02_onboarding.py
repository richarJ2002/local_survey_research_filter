"""
Qualify explicit online previews and atomic, provider-free local saves.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_wp02_onboarding.py

@Brief:  Qualify explicit online previews and atomic, provider-free local saves.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from unittest.mock import patch

# Custom library Imports
import httpx
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from literature.models import (
    Discovery, Paper, PaperKeywordAssignment, Project, Search, SearchRun, Stage,
    SurveyCitation, SurveyEntry,
)
from literature.services.paper_lookup_service import save_entry

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class OnlineBoundaryTests(TestCase):
    """
    Use synthetic metadata only; no test may contact a real provider.
    """

    def setUp(self) -> None:
        """
        Create current settings without seeding research or screening rows.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        self.project = Project.objects.create(name="Synthetic onboarding")
        self.search = Search.objects.create(
            project=self.project, name="Settings", is_current=True,
            keywords=["synthetic"],
        )
        self.metadata = {
            "doi": "10.1000/preview", "title": "Synthetic preview",
            "abstract": "Synthetic abstract", "authors": [],
            "references": ["https://doi.org/10.1000/older"],
        }

    def test_local_gets_fetch_nothing_and_do_not_repair_stages(self) -> None:
        """
        Keep all local previews, figures and reports free of hidden writes.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        routes = (
            "project_detail", "papers_view", "paper_add", "paper_lookup",
            "survey_entry_add", "plots_view", "export_markdown",
        )
        with (
            patch("literature.services.reference_service.lookup_doi") as normal,
            patch("literature.survey_views.lookup_doi") as curated,
            CaptureQueriesContext(connection) as queries,
        ):
            for route in routes:
                with self.subTest(route=route):
                    response = self.client.get(
                        reverse(route, args=[self.project.pk]),
                        {"doi": self.metadata["doi"]},
                    )
                    self.assertEqual(response.status_code, 200)
        normal.assert_not_called()
        curated.assert_not_called()
        self.assertFalse(Stage.objects.exists())
        self.assertFalse(Paper.objects.exists())
        self.assertFalse(SurveyEntry.objects.exists())
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "REPLACE"))
            for query in queries
        ))

    def test_explicit_fetches_return_unsaved_previews(self) -> None:
        """
        Fetch through POST only, preserving a separate save confirmation.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for route, owner in (
            ("paper_lookup", "literature.services.reference_service"),
            ("survey_entry_add", "literature.survey_views"),
        ):
            with self.subTest(route=route), patch(
                owner + ".lookup_doi", return_value=self.metadata,
            ) as fetch:
                url = reverse(route, args=[self.project.pk])
                local = self.client.get(url, {"doi": self.metadata["doi"]})
                fetch.assert_not_called()
                self.assertContains(local, "Fetch DOI metadata — online")
                online = self.client.post(
                    url, {"action": "fetch", "doi": self.metadata["doi"]})
                fetch.assert_called_once_with(self.metadata["doi"])
                self.assertContains(online, "Synthetic preview")
                self.assertContains(online, "preview_token")
                self.assertFalse(Paper.objects.exists())
                self.assertFalse(SurveyEntry.objects.exists())
                self.assertFalse(Stage.objects.exists())

    def test_online_failures_preserve_retry_and_csrf(self) -> None:
        """
        Refuse forged fetches and disclose provider failure without saving.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        client = Client(enforce_csrf_checks=True)
        for route, owner in (
            ("paper_lookup", "literature.services.reference_service"),
            ("survey_entry_add", "literature.survey_views"),
        ):
            with self.subTest(route=route), patch(
                owner + ".lookup_doi",
                side_effect=httpx.RequestError("synthetic failure"),
            ) as fetch:
                url = reverse(route, args=[self.project.pk])
                data = {"action": "fetch", "doi": self.metadata["doi"]}
                self.assertEqual(client.post(url, data).status_code, 403)
                fetch.assert_not_called()
                client.get(url, {"doi": self.metadata["doi"]})
                token = client.cookies["csrftoken"].value
                response = client.post(url, data, HTTP_X_CSRFTOKEN=token)
                self.assertContains(response, "nothing was saved")
                fetch.assert_called_once()
                self.assertFalse(Paper.objects.exists())
                self.assertFalse(SurveyEntry.objects.exists())

    def test_failed_manual_finish_rolls_back_every_record(self) -> None:
        """
        Roll back creation and stage bootstrap if stale provenance is refused.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        data = {
            "action": "manual", "title": "Entered locally",
            "doi": "10.1000/manual", "abstract": "Entered summary",
            "search_keyword": f"{self.search.pk}|synthetic",
            "origin_source": "crossref",
        }
        with (
            patch("literature.views.finish_manual_addition",
                  side_effect=ValueError("Synthetic stale keyword")),
            patch("literature.services.reference_service.enrich_papers")
            as provider,
        ):
            response = self.client.post(
                reverse("paper_add", args=[self.project.pk]), data)
        self.assertContains(response, "Synthetic stale keyword")
        provider.assert_not_called()
        for model in (Paper, Discovery, SearchRun, Stage):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_new_keyword_cannot_replace_a_query_only_configuration(self) -> None:
        """
        Leave adopted query syntax intact until explicit settings translation.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        query = "alpha OR (beta AND NOT gamma)"
        Search.objects.filter(pk=self.search.pk).update(
            keywords=[], query=query)
        values = {
            "search_keyword": "NEW", "new_keyword_search": self.search.pk,
            "new_keyword": "delta", "origin_source": "crossref",
            "title_decision": "ACCEPT", "abstract_decision": "ACCEPT",
        }
        with self.assertRaisesMessage(ValueError, "Translate or replace"):
            save_entry(self.project, self.metadata["doi"], self.metadata,
                       values, None)
        self.search.refresh_from_db()
        self.assertEqual(
            (self.search.query, self.search.keywords), (query, []))
        for model in (
            Paper, Discovery, SearchRun, Stage, PaperKeywordAssignment,
            SurveyEntry, SurveyCitation,
        ):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_dashboard_displays_effective_legacy_provider_defaults(self) -> None:
        """
        Show the four runnable providers even for an invalid stored selection.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        Search.objects.filter(pk=self.search.pk).update(
            sources_selected=["invalid"])
        response = self.client.get(
            reverse("project_detail", args=[self.project.pk]))
        self.assertEqual(response.context["current_search"].display_providers,
                         ["openalex", "semantic_scholar", "crossref", "arxiv"])
        self.assertContains(
            response, "sources: openalex, semantic_scholar, crossref, arxiv")

    def test_existing_survey_does_not_require_another_search(self) -> None:
        """
        Preserve the writing-stage next action before or after adoption.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        paper = Paper.objects.create(title="Previously screened paper")
        Discovery.objects.create(search=self.search, paper=paper)
        url = reverse("project_detail", args=[self.project.pk])
        self.assertContains(self.client.get(url),
                            "finding more papers is optional")
        Search.objects.filter(pk=self.search.pk).update(is_current=False)
        self.assertContains(self.client.get(url),
                            "continue reviewing, linking and exporting now")
        self.assertEqual(Paper.objects.count(), 1)

    def test_historical_link_remains_usable_without_current_settings(self) -> None:
        """
        Separate new-keyword guidance from preserving an existing saved link.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        paper = Paper.objects.create(title="Saved entry", doi="10.1000/saved")
        Discovery.objects.create(search=self.search, paper=paper,
                                 source="crossref")
        assignment = PaperKeywordAssignment.objects.create(
            paper=paper, project=self.project, search=self.search,
            keyword="synthetic")
        Search.objects.filter(pk=self.search.pk).update(is_current=False)
        response = self.client.get(
            reverse("paper_lookup", args=[self.project.pk]), {"doi": paper.doi})
        self.assertFalse(response.context["has_current_search"])
        self.assertTrue(response.context["has_searches"])
        self.assertContains(
            response, "existing saved linkage can remain unchanged")
        assignment.refresh_from_db()
        self.assertEqual((assignment.search_id, assignment.keyword),
                         (self.search.pk, "synthetic"))
