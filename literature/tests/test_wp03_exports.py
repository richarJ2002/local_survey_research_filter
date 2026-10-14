"""Verify shared WP03 image controls and read-only chart boundaries.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File: test_wp03_exports.py
@Brief: Synthetic export controls, hidden axes, preserved records and options.
@Date: 14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import re
from unittest.mock import patch

# Custom library Imports
from django.test import TestCase
from django.urls import reverse

from literature.models import (
    Discovery,
    Paper,
    Project,
    ScreeningDecision,
    Search,
    SearchRun,
    SurveyCitation,
    SurveyEntry,
)

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class WP03ExportTests(TestCase):
    """Use only fictional runner-owned projects, with provider traffic blocked."""

    def setUp(self) -> None:
        """Build a small selected-paper graph and independent curated snapshots.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        self.project = Project.objects.create(name="Fictional <WP03> project")
        self.project.ensure_default_stages()
        search = Search.objects.create(
            project=self.project, name="Synthetic criteria", is_current=True,
            keywords=["fictional"], sources_selected=["crossref"])
        run = SearchRun.objects.create(
            search=search, source="crossref", status="completed",
            results_count=4, duplicates_count=2)
        entries = []
        for index in range(2):
            doi = f"10.1000/wp03-{index}"
            paper = Paper.objects.create(
                title=f"Fictional paper {index}", doi=doi,
                publication_year=2024 + index, abstract="Synthetic abstract",
                reference_text="Synthetic references", sources=["crossref"],
                references=[f"10.1000/wp03-{1-index}"])
            Discovery.objects.create(
                paper=paper, search=search, search_run=run, source="crossref")
            for stage in self.project.stages.all():
                ScreeningDecision.objects.create(
                    paper=paper, stage=stage, decision="ACCEPT")
            entries.append(SurveyEntry.objects.create(
                project=self.project, title=paper.title, doi=doi,
                reference_number=index + 1, publication_year=2024 + index))
        SurveyCitation.objects.create(citing=entries[1], cited=entries[0])
        network = patch("socket.socket", side_effect=AssertionError(
            "WP03 chart GET must not access providers"))
        network.start()
        self.addCleanup(network.stop)

    def test_all_app_charts_share_accessible_export_controls(self) -> None:
        """Cover all seven dashboard charts and the standalone curated page.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for route, count in (("project_detail", 7), ("plots_view", 7),
                             ("survey_graph", 1)):
            with self.subTest(route=route):
                response = self.client.get(reverse(
                    route, args=[self.project.pk]))
                html = response.content.decode()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(html.count("data-chart-export data-"), count)
                self.assertEqual(html.count('data-chart-action="download"'),
                                 count)
                ids = re.findall(r'id="([^"]+-export-[^"]+)"', html)
                self.assertEqual(len(ids), len(set(ids)))
                for label in re.findall(
                        r'for="([^"]+-export-[^"]+)"', html):
                    self.assertIn(label, ids)
                for setting in ("title", "fontFamily", "fontSize",
                                "titleFontFamily", "titleFontSize",
                                "widthMm", "heightMm", "dpi", "preset"):
                    self.assertEqual(html.count(
                        f'data-export-setting="{setting}"'), count)
                self.assertIn("IEEE Access", html)
                self.assertIn("IEEE Robotics Society", html)
                self.assertIn("not formal publication compliance", html)
                self.assertEqual(html.count("data-export-preview alt="), count)
                self.assertIn("chart_navigation.js", html)
                self.assertNotIn("Audit / History", html)
                self.assertNotIn("Current charts page", html)
                for kind in ("flow", "citation", "curated-survey-plot"):
                    key = kind if kind == "curated-survey-plot" else "chart-" + kind
                    self.assertNotIn(f'id="{key}-export-x-title"', html)
                    self.assertNotIn(f'id="{key}-export-y-title"', html)
                self.assertContains(response, "Fictional &lt;WP03&gt; project")

    def test_legacy_history_queries_and_export_options_never_write(self) -> None:
        """Render ordinary charts for obsolete lenses without changing audits.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        models = (Project, Paper, Search, SearchRun, Discovery,
                  ScreeningDecision, SurveyEntry, SurveyCitation)
        before = [list(model.objects.order_by("pk").values())
                  for model in models]
        for route in ("project_detail", "plots_view", "plots_download"):
            response = self.client.get(reverse(route, args=[self.project.pk]), {
                "lens": "history", "history_page": 3,
                "year_from": 2025, "year_to": 2026,
                "yearly_step": "final", "depth": 1})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Current papers by source")
            self.assertNotContains(response, "Audit / History")
            self.assertNotContains(response, 'id="search-history"')
            if route != "plots_download":
                self.assertEqual(response.context["chart_options"]["depth"], 1)
                self.assertEqual(response.context["chart_options"]["year_from"],
                                 2025)
            else:
                self.assertEqual(response["Content-Type"], "text/html")
                self.assertEqual(response["Content-Disposition"],
                                 f'attachment; filename="charts-{self.project.pk}.html"')
                self.assertFalse(re.search(
                    r'<script[^>]+src=["\']https?://',
                    response.content.decode()))
        self.assertEqual([list(model.objects.order_by("pk").values())
                          for model in models], before)

    def test_empty_curated_graph_has_no_axes_title_or_invented_nodes(self) -> None:
        """Keep the empty canvas blank with the same hidden-axis renderer.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        empty = Project.objects.create(name="Fictional empty project")
        with patch("plotly.graph_objects.Figure.to_html", autospec=True,
                   return_value="synthetic figure") as renderer:
            response = self.client.get(
                reverse("survey_graph", args=[empty.pk]))
        self.assertEqual(response.status_code, 200)
        figure = renderer.call_args.args[0]
        self.assertEqual(len(figure.data), 0)
        self.assertFalse(figure.layout.xaxis.visible)
        self.assertFalse(figure.layout.yaxis.visible)
        self.assertIsNone(figure.layout.title.text)
        self.assertEqual(len(figure.layout.annotations), 0)
        self.assertContains(response, "data-chart-export")
        self.assertFalse(empty.survey_entries.exists())
