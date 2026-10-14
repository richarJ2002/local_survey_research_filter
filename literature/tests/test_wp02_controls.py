"""Qualify WP02 controls with isolated synthetic data.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   test_wp02_controls.py

@Brief:  Qualify WP02 controls with isolated synthetic data.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
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
)
from literature.services.metrics_service import project_revision

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes


class InlineChartTests(TestCase):
    """Reuse the reviewed IN/THROUGH and option-parity synthetic gates."""

    def setUp(self) -> None:
        """Create dated and undated final/pending synthetic selections.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        self.project = Project.objects.create(name="Chart <example>")
        self.project.ensure_default_stages()
        search = Search.objects.create(
            project=self.project, name="History", keywords=["synthetic"])
        for index, year in enumerate((2020, 2021, 2022, None)):
            paper = Paper.objects.create(
                title=f"Synthetic {index}", doi=f"10.1000/chart-{index}",
                publication_year=year, sources=["crossref", "openalex"],
                reference_text="Bibliography" if index != 1 else "")
            Discovery.objects.create(search=search, paper=paper,
                                     source="crossref")
            for stage in self.project.stages.all():
                ScreeningDecision.objects.create(
                    stage=stage, paper=paper, decision="ACCEPT")

    def test_six_controls_preserve_normalized_options(self) -> None:
        """Check every card preserves the other cards' exact settings.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        query = {
            "keyword_order": "found_desc", "keyword_step": "final",
            "source_order": "name", "flow_counts": "off",
            "yearly_step": "final", "totals_step": "final",
            "year_from": 2021, "year_to": 2022, "stage": "found",
            "depth": 3, "chain": 1, "prune": "on",
        }
        for route in ("project_detail", "plots_view"):
            response = self.client.get(
                reverse(route, args=[self.project.pk]), query)
            self.assertContains(response, 'class="chart-controls"', count=6)
            for card in response.context["chart_cards"]:
                own = {control["name"] for control in card["controls"]}
                self.assertEqual(set(card["preserved"]) | own, set(query))
            self.assertIn("year_from=2021", response.context["chart_query"])
            self.assertIn("prune=on", response.context["chart_query"])

    def test_window_totals_and_final_popup_parity(self) -> None:
        """Check THROUGH includes earlier years and excludes pending refs.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        query = {"year_from": 2021, "year_to": 2022,
                 "yearly_step": "final", "totals_step": "final"}
        with patch("plotly.graph_objects.Figure.to_html", autospec=True,
                   return_value="figure") as renderer:
            response = self.client.get(
                reverse("project_detail", args=[self.project.pk]), query)
        figures = [call.args[0] for call in renderer.call_args_list]
        self.assertEqual(len(figures), 7)
        yearly, totals = figures[3:5]
        self.assertEqual(len(yearly.data), 1)
        self.assertEqual(list(yearly.data[0].y), [0, 1])
        self.assertEqual(list(totals.data[0].y), [1, 2])
        for year, count in zip(totals.data[0].x, totals.data[0].y):
            point = self.client.get(
                reverse("chart_entries", args=[self.project.pk]),
                {"step": "abstract", "mode": "through", "year": year,
                 "format": "json"}).json()
            self.assertEqual(point["count"], count)
        self.assertEqual(response.context["metrics"]["reference_pending"], 1)
        self.assertEqual(list(totals.data[0].customdata), [
            ["abstract", "through", 2021], ["abstract", "through", 2022]])

    def test_display_download_parity_and_modebars(self) -> None:
        """Check identical app/export data and retained offline modebars.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        query = {"keyword_step": "title", "source_order": "count_desc",
                 "flow_counts": "off", "year_from": 2021,
                 "totals_step": "final", "yearly_step": "found", "depth": 0}
        captured = []
        for route in ("project_detail", "plots_view", "plots_download"):
            with patch("plotly.graph_objects.Figure.to_html", autospec=True,
                       return_value="figure") as renderer:
                response = self.client.get(
                    reverse(route, args=[self.project.pk]), query)
            figures = renderer.call_args_list
            self.assertEqual(len(figures), 6 if route ==
                             "plots_download" else 7)
            captured.append([call.args[0].to_plotly_json()["data"]
                             for call in figures[:6]])
            for call in figures[:6]:
                config = call.kwargs.get("config") or {}
                self.assertEqual(config.get("displayModeBar"),
                                 None if route == "plots_download" else False)
            if route != "plots_download":
                self.assertEqual(
                    figures[6].kwargs.get("config", {}).get("displayModeBar"),
                    False)
        self.assertEqual(captured[0], captured[1])
        self.assertEqual(captured[0], captured[2])
        self.assertContains(response, "Chart &lt;example&gt;")
        self.assertNotContains(response, "Chart <example>")

    def test_invalid_empty_windows_are_local(self) -> None:
        """Check invalid feedback, empty windows and unchanged stored data.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        before = project_revision(self.project)
        query = {"keyword_step": "bad", "source_order": "bad",
                 "depth": "abc", "year_from": 9999, "year_to": 1}
        for route in ("project_detail", "plots_download"):
            response = self.client.get(
                reverse(route, args=[self.project.pk]), query)
            self.assertContains(response, "Invalid keyword_step")
            self.assertContains(response, "From year is later")
        empty = self.client.get(
            reverse("project_detail", args=[self.project.pk]),
            {"year_from": 2050})
        self.assertTrue(empty.context["chart_window_empty"])
        self.assertContains(empty, "No dated papers in this window")
        self.assertEqual(project_revision(self.project), before)

    def test_metrics_reused_and_dashboard_includes_curated(self) -> None:
        """Check dashboard constructs metrics once and restores curation.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.survey_graph_service import build_survey_graph
        from literature.views import build_metrics

        with (
            patch("literature.views.build_metrics",
                  wraps=build_metrics) as metrics_builder,
            patch("literature.services.survey_graph_service."
                  "build_survey_graph", wraps=build_survey_graph) as curated,
        ):
            response = self.client.get(
                reverse("project_detail", args=[self.project.pk]))
        self.assertEqual(metrics_builder.call_count, 1)
        self.assertEqual(curated.call_count, 1)
        self.assertContains(response, 'id="curated-survey-section"')

    def test_curated_graph_uses_same_renderer_and_dashboard_navigation(self) -> None:
        """Draw chosen entries with the same Plotly style and view controls.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.models import SurveyEntry

        initial = self.client.get(
            reverse("project_detail", args=[self.project.pk]))
        empty_panel = initial.content.decode().split(
            'id="curated-survey-plot"', 1)[1].split(
                'id="curated-survey-entries"', 1)[0]
        self.assertContains(initial, "It starts empty until you add one")
        self.assertIn('data-chart-action="download"', empty_panel)
        self.assertNotIn('data-chart-action="out"', empty_panel)
        SurveyEntry.objects.create(
            project=self.project, reference_number=7, title="Selected entry",
            origin_source="openalex", publication_year=2020)
        with patch("plotly.graph_objects.Figure.to_html", autospec=True,
                   return_value="figure") as renderer:
            response = self.client.get(
                reverse("project_detail", args=[self.project.pk]))
        self.assertEqual(len(renderer.call_args_list), 7)
        automatic, curated = [call.args[0]
                              for call in renderer.call_args_list[5:7]]
        self.assertEqual(automatic.layout.dragmode, curated.layout.dragmode)
        self.assertEqual(automatic.layout.legend.title.text,
                         curated.layout.legend.title.text)
        for figure in (automatic, curated):
            self.assertTrue(any(trace.meta and trace.meta.get("paper_details")
                                and trace.marker.color == "#3f7e3f"
                                for trace in figure.data))
        section = response.content.decode().split(
            'id="curated-survey-plot"', 1)[1].split("</section>", 1)[0]
        for action in ("download", "reset"):
            self.assertIn(f'data-chart-action="{action}"', section)
        for route in ("project_detail", "plots_view", "survey_graph"):
            with self.subTest(route=route):
                page = self.client.get(
                    reverse(route, args=[self.project.pk]))
                self.assertContains(page, "chart_navigation.js")
                self.assertContains(page, 'data-chart-action="download"')
                self.assertContains(page, 'data-chart-action="reset"')
                for action in ("in", "out", "pan"):
                    self.assertNotContains(
                        page, f'data-chart-action="{action}"')
        self.assertContains(response, "Selected entry")

    def test_citation_controls_only_offer_movement_with_nodes(self) -> None:
        """Offer only download and full-graph reset when papers are present.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        url = reverse("project_detail", args=[self.project.pk])
        populated = self.client.get(url)
        self.assertGreater(populated.context["citation_nodes"], 0)
        section = populated.content.decode().split('id="chart-citation"', 1)[1]
        section = section.split("</section>", 1)[0]
        for action in ("download", "reset"):
            self.assertIn(f'data-chart-action="{action}"', section)
        for action in ("in", "out", "pan"):
            self.assertNotIn(f'data-chart-action="{action}"', section)
        ScreeningDecision.objects.filter(
            stage__project=self.project, stage__name="Abstract").update(
                decision="REJECT")
        empty = self.client.get(url)
        self.assertEqual(empty.context["citation_nodes"], 0)
        section = empty.content.decode().split('id="chart-citation"', 1)[1]
        section = section.split("</section>", 1)[0]
        self.assertIn('data-chart-action="download"', section)
        for action in ("in", "out", "pan", "reset"):
            self.assertNotIn(f'data-chart-action="{action}"', section)

    def test_default_and_single_step_totals_menus(self) -> None:
        """Check native series controls never leave obsolete trace masks.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.metrics_service import stage_year_cumulative
        from literature.services.plots_service import (
            build_stage_cumulative_figure,
        )

        for options, menus, traces in (({}, 1, 3),
                                       ({"inline_controls": True}, 0, 3),
                                       ({"step": "final"}, 0, 1)):
            with patch("plotly.graph_objects.Figure.to_html", autospec=True,
                       return_value="figure") as renderer:
                build_stage_cumulative_figure(
                    stage_year_cumulative(self.project), **options)
            figure = renderer.call_args.args[0]
            self.assertEqual(len(figure.layout.updatemenus), menus)
            self.assertEqual(len(figure.data), traces)

    def test_ordering_flow_categories_and_default_options(self) -> None:
        """Check useful order/count controls preserve default figure data.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.services.plots_service import (
            build_funnel_figure,
            build_keyword_figure,
            current_source_figure,
        )
        from literature.views import _chart_options

        options = _chart_options()
        self.assertEqual(options, {
            "keyword_order": "az", "keyword_step": "all",
            "source_order": "canonical", "flow_counts": "on",
            "yearly_step": "all", "totals_step": "all", "stage": "final",
            "prune": "", "depth": 2, "chain": 1,
            "year_from": None, "year_to": None, "feedback": [],
        })
        counts = {"Alpha": {"found": 1, "title": 1, "abstract": 0},
                  "Beta": {"found": 3, "title": 2, "abstract": 1}}
        for order, expected in (("az", ["Alpha", "Beta"]),
                                ("found_desc", ["Beta", "Alpha"])):
            with patch("plotly.graph_objects.Figure.to_html", autospec=True,
                       return_value="figure") as renderer:
                build_keyword_figure(counts, order=order, step="final")
            figure = renderer.call_args.args[0]
            self.assertEqual(list(figure.data[0].x), expected)
            self.assertEqual(len(figure.data), 1)
        current = {"unique": 4, "blank": 1,
                   "per_source": {"openalex": 2, "crossref": 3}}
        default = current_source_figure(current)
        self.assertEqual(list(default.data[0].x), ["OpenAlex", "Crossref"])
        reordered = current_source_figure(current, order="count_desc")
        self.assertEqual(list(reordered.data[0].y), [3, 2])
        self.assertEqual(list(current_source_figure({"unique": 1,
                                                     "blank": 1,
                                                     "per_source": {}}).data), [])
        graphs = []
        for show_counts in (True, False):
            with patch("plotly.graph_objects.Figure.to_html", autospec=True,
                       return_value="figure") as renderer:
                build_funnel_figure({"total_identified": 5,
                                     "unique_total": 4}, counts=show_counts)
            graphs.append(renderer.call_args.args[0].data[0])
        self.assertEqual(graphs[0].node.label, graphs[1].node.label)
        self.assertEqual(graphs[0].link.value, graphs[1].link.value)
        self.assertTrue(all(label == "" for label in graphs[1].link.label))


class PaperOverviewTests(TestCase):
    """Reuse bounded status/paging/projection gates on the existing route."""

    def test_exact_statuses_share_membership_and_escape_text(self) -> None:
        """Check all eight filters and local foreign-project isolation.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Overview")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="Historical")
        cases = {
            "title_pending": ("PENDING", None),
            "title_rejected": ("REJECT", None),
            "abstract_pending": ("ACCEPT", "PENDING"),
            "abstract_rejected": ("ACCEPT", "REJECT"),
            "references_pending": ("ACCEPT", "ACCEPT"),
            "final": ("ACCEPT", "ACCEPT"), "unqueued": (None, None),
        }
        expected = {}
        for status, (title, abstract) in cases.items():
            paper = Paper.objects.create(
                title="<img src=x onerror=alert(1)> " + status,
                abstract=" \u2003INTERNATIONAL audience\u2003 ",
                reference_text="Bibliography" if status == "final" else "",
                sources=["manual", "crossref"])
            Discovery.objects.create(search=search, paper=paper)
            for name, decision in (("Title", title), ("Abstract", abstract)):
                if decision:
                    ScreeningDecision.objects.create(
                        paper=paper, stage=project.stages.get(name=name),
                        decision=decision)
            expected[status] = paper.pk
        foreign = Project.objects.create(name="Foreign")
        foreign.ensure_default_stages()
        other = Search.objects.create(project=foreign, name="Foreign")
        Discovery.objects.create(search=other,
                                 paper=Paper.objects.create(title="Foreign"))
        shared = Paper.objects.get(pk=expected["unqueued"])
        ScreeningDecision.objects.create(
            stage=foreign.stages.get(name="Title"), paper=shared,
            decision="ACCEPT")
        url = reverse("papers_view", args=[project.pk])
        for status, paper_id in expected.items():
            for view in ("hud", "full"):
                response = self.client.get(
                    url, {"view": view, "status": status})
                self.assertEqual([row["paper"].pk
                                  for row in response.context["rows"]],
                                 [paper_id])
                self.assertNotContains(response, "Foreign")
                self.assertContains(response, "&lt;img")
                self.assertNotContains(response, "<img src=x")
                if view == "hud":
                    paper = response.context["rows"][0]["paper"]
                    deferred = paper.get_deferred_fields()
                    required = {"abstract", "references", "reference_text",
                                "authors", "keywords", "raw", "source_ids"}
                    self.assertTrue(required.issubset(deferred))
        all_rows = self.client.get(url, {"view": "hud"})
        self.assertEqual(all_rows.context["page"].paginator.count, 7)
        self.assertEqual({choice["value"] for choice in
                          all_rows.context["status_choices"]},
                         {"all", *cases})
        pending = self.client.get(url, {"view": "hud",
                                        "status": "abstract_pending"})
        self.assertContains(pending, "Pending — abstract needed")
        self.assertEqual(self.client.post(
            url, {"view": "hud"}).status_code, 405)

    def test_paging_filters_sort_and_no_eager_dashboard_rows(self) -> None:
        """Check stable pages and explicit pre-pagination query/filtering.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        project = Project.objects.create(name="Paged")
        search = Search.objects.create(project=project, name="History")
        papers = Paper.objects.bulk_create([
            Paper(title="Tie & match", publication_year=2025)
            for _ in range(35)])
        Discovery.objects.bulk_create([
            Discovery(search=search, paper=paper) for paper in papers])
        # A second discovery must not duplicate the row or count.
        Discovery.objects.create(
            search=search, paper=papers[0], source="arxiv")
        url = reverse("papers_view", args=[project.pk])
        with (
            patch("literature.services.metrics_service.project_revision",
                  side_effect=AssertionError("HUD hashed whole project")),
            patch("literature.views._plots_context",
                  side_effect=AssertionError("HUD rebuilt plots")),
        ):
            pages = [self.client.get(url, {
                "view": "hud", "page": number, "q": "Tie &",
                "status": "unqueued", "sort": "title", "direction": "asc",
            }) for number in (1, 2)]
        self.assertEqual([len(response.context["rows"]) for response in pages],
                         [25, 10])
        self.assertEqual([row["paper"].pk for response in pages
                          for row in response.context["rows"]],
                         [paper.pk for paper in papers])
        self.assertEqual(project.stages.count(), 0)
        self.assertContains(pages[0], "35 matching papers; showing 1–25")
        self.assertContains(pages[0], "Project total: 35")
        for value in ("not-a-page", -1, 999):
            response = self.client.get(url, {"view": "hud", "page": value})
            self.assertLessEqual(len(response.context["rows"]), 25)
        invalid = self.client.get(url, {
            "view": "hud", "status": "bad", "sort": "DROP TABLE",
            "direction": "bad"})
        self.assertContains(invalid, "Unknown paper status")
        self.assertEqual(invalid.context["sort"], "year")
        empty = self.client.get(url, {"view": "hud", "q": "no matches"})
        self.assertContains(empty, "No papers match these filters")
        self.assertNotContains(empty, "No stored papers yet")
        project.ensure_default_stages()
        dashboard = self.client.get(
            reverse("project_detail", args=[project.pk]))
        self.assertContains(dashboard, 'id="paper-overview"')
        self.assertContains(dashboard, 'href="' + url + '">Paper overview</a>')
        self.assertNotContains(dashboard, 'data-paper-id="')

    def test_10000_papers_bounded_sql_projection_and_payload(self) -> None:
        """Measure core SQLite reads, elapsed time and bounded 25-row payloads.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        import time

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        project = Project.objects.create(name="10k synthetic")
        search = Search.objects.create(project=project, name="History")
        papers = Paper.objects.bulk_create([
            Paper(title=f"Large {index:05} " + "long title " * 50,
                  publication_year=2025, sources=["manual", "crossref"],
                  abstract="PRIVATE_BLOB_NOT_FOR_HUD" * 50,
                  reference_text="PRIVATE_BIBLIOGRAPHY" * 50,
                  raw={"payload": "RAW_NOT_FOR_HUD" * 50})
            for index in range(10000)], batch_size=500)
        Discovery.objects.bulk_create([
            Discovery(search=search, paper=paper) for paper in papers],
            batch_size=500)
        url = reverse("papers_view", args=[project.pk])
        for params in ({"page": 1}, {"page": 400},
                       {"q": "Large", "status": "unqueued",
                        "sort": "abstract_status"}):
            started = time.perf_counter()
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url, {"view": "hud", **params})
            elapsed = time.perf_counter() - started
            self.assertEqual(response.context["page"].paginator.count, 10000)
            self.assertEqual(len(response.context["rows"]), 25)
            self.assertLessEqual(len(queries), 8)
            self.assertLess(len(response.content), 60000)
            for marker in ("PRIVATE_BLOB_NOT_FOR_HUD", "PRIVATE_BIBLIOGRAPHY",
                           "RAW_NOT_FOR_HUD"):
                self.assertNotContains(response, marker)
            for sql in queries.captured_queries:
                self.assertNotIn('"literature_paper"."raw"', sql["sql"])
                self.assertTrue(
                    sql["sql"].lstrip().upper().startswith("SELECT"))
            print(f"WP02 HUD evidence: corpus=10000 rows=25 "
                  f"params={params} reads={len(queries)} "
                  f"bytes={len(response.content)} seconds={elapsed:.4f}")
