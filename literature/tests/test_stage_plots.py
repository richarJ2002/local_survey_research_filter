#!/usr/bin/env python3

"""Verify cumulative stage counts, adaptive axes, and plot downloads.

@File:   test_stage_plots.py

@Brief:  Tests for per-stage running totals and download-all.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems
    (AERO-Sys) group.

"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

from unittest.mock import patch

# Custom library Imports
from django.test import TestCase

from literature.models import (
    Discovery, Paper, Project, ScreeningDecision, Search,
)
from literature.services.metrics_service import stage_year_cumulative
from literature.services.plots_service import build_stage_cumulative_figure

# ------------------------------------------------------------------------ #
# Stage plot tests
# ------------------------------------------------------------------------ #


class StagePlotTests(TestCase):
    """Running totals split by step, undated counted aside."""

    # --- Setup helper ---
    def _setup(self) -> Project:
        """Create discovered papers with dated and undated Title decisions.

        Two dated papers (one kept) plus one undated.

        Args:
            None

        Kwargs:
            None

        Returns:
            Project: Project with three discoveries: a kept 2020 paper, a
                rejected 2021 paper, and a kept undated paper.
        """
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(
            project=project, name="S", keywords=["a"])
        p1 = Paper.objects.create(title="A", publication_year=2020)
        p2 = Paper.objects.create(title="B", publication_year=2021)
        p3 = Paper.objects.create(title="C", publication_year=None)
        for paper in (p1, p2, p3):
            Discovery.objects.create(
                paper=paper, search=search, source="openalex", query="a")
        title = project.stages.get(name="Title")
        ScreeningDecision.objects.create(
            paper=p1, stage=title, decision="ACCEPT")
        ScreeningDecision.objects.create(
            paper=p2, stage=title, decision="REJECT")
        ScreeningDecision.objects.create(
            paper=p3, stage=title, decision="ACCEPT")
        return project

    # --- Series ---
    def test_series_and_footnote(self) -> None:
        """Count dated cumulative stages and show undated papers in a footnote.

        Lines rise; undated counted in footnote only.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = self._setup()
        data = stage_year_cumulative(project)
        self.assertEqual(data["years"], [2020, 2021])
        self.assertEqual(data["series"]["found"], [1, 2])
        self.assertEqual(data["series"]["title"], [1, 1])
        self.assertEqual(data["undated"]["found"], 1)
        html = build_stage_cumulative_figure(data)
        self.assertIn("Running totals by year", html)
        self.assertIn("no year", html)

    # --- Axis + footnote layout ---
    def test_adaptive_ticks_and_footnote_clearance(self) -> None:
        """Check yearly ticks and footnote spacing for a short year range.

        A short span has yearly ticks and fixed footnote clearance below the
        axis.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = self._setup()
        data = stage_year_cumulative(project)
        compact = build_stage_cumulative_figure(data).replace(" ", "")
        self.assertIn('"dtick":1', compact)
        self.assertIn('"tick0":2020', compact)
        self.assertIn('"b":180', compact)
        self.assertIn('"yshift":-100', compact)

    def test_tick_intervals_adapt_without_resampling_data(self) -> None:
        """Use 1/2/5/10-year ticks, capped at ten even for centuries of data.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for years, interval in (
            ([2024], 1), ([2020, 2025], 1), ([2001, 2013], 2),
            ([1999, 2025], 5), ([1900, 2025], 10),
        ):
            with self.subTest(years=years):
                with patch(
                    "plotly.graph_objects.Figure.to_html", autospec=True,
                    return_value="chart",
                ) as render:
                    build_stage_cumulative_figure({
                        "years": years,
                        "series": {"found": list(range(len(years)))},
                    })
                figure = render.call_args.args[0]
                self.assertEqual(figure.layout.xaxis.dtick, interval)
                self.assertLessEqual(figure.layout.xaxis.dtick, 10)
                self.assertEqual(list(figure.data[0].x), years)

    def test_undated_only_chart_keeps_counts_in_a_separate_footnote(
        self,
    ) -> None:
        """Display undated-only counts with the same footnote clearance.

        Even without any dated observations, the omitted-paper counts remain
        visible.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        with patch(
            "plotly.graph_objects.Figure.to_html", autospec=True,
            return_value="chart",
        ) as render:
            build_stage_cumulative_figure({
                "years": [],
                "undated": {"found": 4, "title": 3, "abstract": 2},
            })
        figure = render.call_args.args[0]
        self.assertIn("4 papers have no year",
                      figure.layout.annotations[0].text)
        self.assertIn("final selected: 2", figure.layout.annotations[0].text)
        self.assertEqual(figure.layout.annotations[0].yshift, -100)
        self.assertEqual(figure.layout.margin.b, 180)

    # --- Dashboard + download ---
    def test_plots_and_download(self) -> None:
        """Render the stage chart and return an attachment from plot download.

        Dashboard shows stage chart; download returns one file.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = self._setup()
        resp = self.client.get(f"/project/{project.pk}/plots/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Running totals by year at each step",
                      resp.content.decode())
        dl = self.client.get(f"/project/{project.pk}/plots/download/")
        self.assertEqual(dl.status_code, 200)
        self.assertIn("attachment", dl["Content-Disposition"])
