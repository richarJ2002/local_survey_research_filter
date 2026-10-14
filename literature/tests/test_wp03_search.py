"""Verify WP03 Search presentation and its existing safe action boundaries.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File: test_wp03_search.py
@Brief: Synthetic-only checks for Search controls, previews and confirmation.
@Date: 14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import re
from unittest.mock import patch

# Custom library Imports
from django.conf import settings
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from literature.models import (
    Discovery,
    Paper,
    Project,
    ScreeningDecision,
    Search,
    SearchRun,
)
from literature.views import project_detail

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Synthetic Search-page regression checks ---


@override_settings(OPENALEX_EMAIL="wp03@example.invalid")
class WP03SearchTests(TestCase):
    """Exercise only runner-owned fictional data and refuse provider traffic."""

    def setUp(self) -> None:
        """Create current criteria, retained audits and reference-gated papers.

        Args:
            None

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Seed the isolated test database and block sockets until cleanup.
        """
        no_network = patch(
            "socket.socket", side_effect=AssertionError("No WP03 network"))
        no_network.start()
        self.addCleanup(no_network.stop)
        self.project = Project.objects.create(name="Synthetic WP03 survey")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(
            project=self.project, name="Current criteria", is_current=True,
            keywords=["navigation", "battery"], year_min=2020, year_max=2026,
            venues=["Synthetic journal"], author_names=["Synthetic author"],
            sources_selected=["crossref"],
        )
        historical = Search.objects.create(
            project=self.project, name="Retained historical criteria")
        self.run = SearchRun.objects.create(
            search=historical, source="crossref", status="failed",
            error_message="Synthetic provider timeout <retry>",
        )
        SearchRun.objects.create(
            search=historical, source="crossref", status="completed",
            results_count=9, duplicates_count=7,
        )
        for index, bibliography in enumerate(("Synthetic bibliography", "")):
            paper = Paper.objects.create(
                title=f"Synthetic paper {index}", abstract="Synthetic summary",
                publication_year=2025, reference_text=bibliography,
            )
            Discovery.objects.create(
                paper=paper, search=self.search, source="crossref")
            for stage in self.project.stages.all():
                ScreeningDecision.objects.create(
                    paper=paper, stage=stage, decision="ACCEPT")
        self.url = reverse("project_detail", args=[self.project.pk])

    def _search_section(self, html: str) -> str:
        """Isolate Search markup without inspecting charts or their scripts.

        Args:
            html (str): Rendered project dashboard containing the Search card.

        Kwargs:
            None

        Returns:
            str: Search-card content up to its closing section tag.
        """
        return html.split('id="search-section"', 1)[1].split("</section>", 1)[0]

    def test_stacked_actions_preview_results_and_audits(self) -> None:
        """Order the four controls while preserving criteria, counts and data.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        models = (Project, Search, SearchRun, Paper, Discovery,
                  ScreeningDecision)
        before = [list(model.objects.order_by("pk").values())
                  for model in models]
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, {"history_page": 2})
        self.assertEqual(response.status_code, 200)
        section = self._search_section(response.content.decode())
        actions = section.split('id="search-actions"', 1)[1].split(
            "</div>", 1)[0]
        self.assertEqual(re.findall(
            r"<(?:a|button)\b[^>]*>([^<]+)</(?:a|button)>", actions), [
                "Manage API settings", "Configure search criteria",
                "Find papers now", "Add papers by hand",
        ])
        self.assertIn("display: grid;", actions)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", actions)
        for route in ("search_create", "paper_add"):
            self.assertIn(
                f'href="{reverse(route, args=[self.project.pk])}"', actions)
        self.assertIn(f'href="{reverse("settings_view")}"', actions)
        self.assertIn(
            'action="' + reverse("search_run", args=[
                self.project.pk, self.search.pk]) + '" data-search-run>',
            actions,
        )
        for preview in (
            'id="search-preview"', "sources: crossref",
            "Keywords (A–Z): battery, navigation", "Years: 2020–2026",
            "Journals: Synthetic journal", "Authors: Synthetic author",
            "Not run yet", "finding more papers is optional",
        ):
            self.assertIn(preview, section)
        self.assertLess(section.index('id="search-actions"'),
                        section.index('id="results-summary"'))
        self.assertEqual(section.count('id="results-summary"'), 1)
        for label, value in (("Records found", 9), ("Unique papers", 2),
                             ("Final kept", 1), ("Awaiting references", 1)):
            self.assertIn(
                f'<th scope="row">{label}</th><td>{value}</td>', section)
        for removed in ("search-history", "Search history", "Search run history",
                        "More actions", "Retained historical criteria"):
            self.assertNotIn(removed, section)
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "REPLACE"))
            for query in queries
        ))
        self.assertEqual(
            [list(model.objects.order_by("pk").values()) for model in models],
            before,
        )

    def test_no_current_criteria_and_empty_project(self) -> None:
        """Disable only finding papers and retain local actions and empty hints.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        Search.objects.filter(pk=self.search.pk).update(is_current=False)
        empty = Project.objects.create(name="Synthetic empty project")
        for project in (self.project, empty):
            with self.subTest(project=project.name):
                response = self.client.get(reverse(
                    "project_detail", args=[project.pk]))
                section = self._search_section(response.content.decode())
                self.assertIn(
                    'disabled aria-describedby="no-current-search"', section)
                self.assertNotIn("data-search-run", section)
                self.assertIn("No current search settings.", section)
                self.assertIn("Add papers by hand</a>", section)
                self.assertIn("Manage API settings</a>", section)
        self.assertContains(
            response, "Configure search criteria before finding papers.")
        self.assertContains(response, "No stored papers yet.")
        self.assertFalse(empty.stages.exists())
        retained = self.client.get(self.url)
        self.assertContains(
            retained, "continue reviewing, linking and exporting now")
        self.assertEqual(SearchRun.objects.get(pk=self.run.pk).error_message,
                         self.run.error_message)

    def test_visible_results_actions_keep_post_and_csrf(self) -> None:
        """Expose four secondary actions without weakening online POST guards.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        response = self.client.get(self.url)
        section = self._search_section(response.content.decode())
        actions = section.split('id="results-actions"', 1)[1].split(
            "</div>", 1)[0]
        self.assertEqual(re.findall(
            r"<(?:a|button)\b[^>]*>([^<]+)</(?:a|button)>", actions), [
                "Review duplicates", "Fetch missing details",
                "Fetch missing citations", "Download Markdown",
        ])
        self.assertLess(section.index("</table>"),
                        section.index('id="results-actions"'))
        self.assertNotIn("<details", actions)
        for route in ("duplicates_view", "export_markdown"):
            self.assertIn(
                f'href="{reverse(route, args=[self.project.pk])}"', actions)
        client = Client(enforce_csrf_checks=True)
        for route, args in (
            ("refs_backfill", [self.project.pk]),
            ("citations_enrich", [self.project.pk]),
            ("search_run", [self.project.pk, self.search.pk]),
        ):
            with self.subTest(route=route):
                url = reverse(route, args=args)
                form = re.search(
                    rf'<form method="post" action="{re.escape(url)}"'
                    r"[^>]*>.*?</form>", section, re.DOTALL)
                self.assertIsNotNone(form)
                self.assertIn('name="csrfmiddlewaretoken"', form.group())
                self.assertEqual(client.get(url).status_code, 405)
                self.assertEqual(client.post(url).status_code, 403)

    def test_title_delete_uses_existing_post_confirmation(self) -> None:
        """Require confirmation and CSRF before deleting a synthetic project.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        response = self.client.get(self.url)
        html = response.content.decode()
        title = html.split('class="card project-summary"', 1)[1].split(
            "</header>", 1)[0]
        url = reverse("project_delete", args=[self.project.pk])
        self.assertIn('class="project-card__header"', title)
        self.assertIn(f'href="{url}">Delete project</a>', title)
        self.assertLess(title.index("</h1>"), title.index("Delete project"))
        self.assertNotIn("Delete project", self._search_section(html))
        client = Client(enforce_csrf_checks=True)
        confirmation = client.get(url)
        self.assertContains(confirmation, "Delete project?")
        self.assertContains(confirmation, 'method="post"')
        self.assertContains(confirmation, 'name="csrfmiddlewaretoken"')
        self.assertContains(confirmation, "Cancel</a>")
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertEqual(client.post(url).status_code, 403)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        other = Project.objects.create(name="Synthetic unrelated project")
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        deleted = client.post(url, {"csrfmiddlewaretoken": token})
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
        self.assertTrue(Project.objects.filter(pk=other.pk).exists())

    def test_configure_preview_and_provider_errors_remain_visible(self) -> None:
        """Keep signed criteria editing and escaped provider failure feedback.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        configure = self.client.get(reverse(
            "search_create", args=[self.project.pk]))
        for field in ("configuration_token", "keywords_json", "venues_json",
                      "author_names_json", "sources_selected"):
            self.assertContains(configure, f'name="{field}"')
        self.assertRegex(configure.content.decode(),
                         r'<input[^>]*value="crossref"[^>]* checked>')
        before = Search.objects.values().get(pk=self.search.pk)
        url = reverse("search_run", args=[self.project.pk, self.search.pk])
        with patch("literature.views.run_search", return_value=[self.run]):
            failed = self.client.post(url, follow=True)
        self.assertContains(
            failed, "crossref: failed (Synthetic provider timeout &lt;retry&gt;)")
        with patch("literature.views.run_search", side_effect=ValueError(
                "Synthetic invalid criteria")):
            invalid = self.client.post(url, follow=True)
        self.assertContains(
            invalid, "Search failed: Synthetic invalid criteria")
        self.assertEqual(Search.objects.values().get(
            pk=self.search.pk), before)
        self.assertEqual(SearchRun.objects.count(), 2)
        for loading in ('role="status" hidden', "Searching selected sources.",
                        'form.dataset.submitting === "true"',
                        "button.disabled = true"):
            self.assertContains(invalid, loading)

    def test_manager_settings_target_and_query_only_preview(self) -> None:
        """Route API settings to the manager and show retained legacy syntax.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        query = 'alpha OR "beta gamma" <legacy>'
        Search.objects.filter(pk=self.search.pk).update(
            keywords=[], query=query)
        request = RequestFactory().get(self.url)
        request.manager_base = "http://127.0.0.1:8001"
        response = project_detail(request, self.project.pk)
        section = self._search_section(response.content.decode())
        self.assertIn(
            'href="http://127.0.0.1:8001/settings/">Manage API settings</a>',
            section,
        )
        self.assertIn(
            'Query: <code>alpha OR &quot;beta gamma&quot; &lt;legacy&gt;</code>',
            section,
        )
        self.search.refresh_from_db()
        self.assertEqual(
            (self.search.query, self.search.keywords), (query, []))
