"""Chart-list filters must match every plotted point.

@File: test_chart_entries.py
@Brief: Yearly/cumulative semantics, pagination and point metadata.
@Date: 02/10/2026
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from literature.models import Discovery, Paper, Project, ScreeningDecision, Search
from literature.services.metrics_service import stage_year_cumulative
from literature.services.plots_service import build_stage_cumulative_figure, build_yearly_figure


# --- Chart boundaries ---
class ChartEntryTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Chart test")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(project=self.project, name="Search", keywords=["test"])
        for index, year in enumerate([2020, 2021, 2021, 2022, None]):
            paper = Paper.objects.create(
                title=f"Paper {index}",
                publication_year=year,
                reference_text="Bibliography" if index in (0, 4) else "",
            )
            Discovery.objects.create(search=self.search, paper=paper, source="crossref")
            # Multiple discoveries must not inflate chart lists.
            Discovery.objects.create(search=self.search, paper=paper, source="openalex")
            for stage in self.project.stages.all():
                ScreeningDecision.objects.create(
                    stage=stage, paper=paper, decision="ACCEPT" if index != 2 else "REJECT"
                )
        other = Project.objects.create(name="Other")
        search = Search.objects.create(project=other, name="Other search")
        Discovery.objects.create(
            search=search,
            paper=Paper.objects.create(title="Foreign", publication_year=2020),
            source="crossref",
        )
        self.url = reverse("chart_entries", args=[self.project.pk])

    def test_all_point_counts_match_plots_and_preview_membership(self):
        data = stage_year_cumulative(self.project)
        for mode, section in (("year", "yearly"), ("through", "series")):
            for step, counts in data[section].items():
                for year, count in zip(data["years"], counts):
                    with self.subTest(mode=mode, step=step, year=year):
                        result = self.client.get(
                            self.url, {"mode": mode, "step": step, "year": year, "format": "json"}
                        ).json()
                        self.assertEqual(result["count"], count)
                        self.assertEqual(len(result["papers"]), count)
                        self.assertNotIn("Foreign", [paper["title"] for paper in result["papers"]])
                        for paper in result["papers"]:
                            self.assertIsNotNone(paper["year"])
                            self.assertLessEqual(paper["year"], year)
                            if mode == "year":
                                self.assertEqual(paper["year"], year)
        final = self.client.get(
            self.url, {"mode": "through", "step": "abstract", "year": 2022, "format": "json"}
        ).json()
        self.assertEqual([paper["title"] for paper in final["papers"]], ["Paper 0"])

    def test_preview_cap_and_stable_pagination(self):
        for index in range(30):
            paper = Paper.objects.create(title=f"Extra {index:02}", publication_year=2021)
            Discovery.objects.create(search=self.search, paper=paper, source="crossref")
        filters = {"year": 2021, "step": "found", "mode": "year"}
        preview = self.client.get(self.url, {**filters, "format": "json"}).json()
        self.assertEqual((preview["count"], len(preview["papers"])), (32, 10))
        first = self.client.get(self.url, filters)
        second = self.client.get(self.url, {**filters, "page": 2})
        self.assertEqual(len(first.context["page"]), 25)
        self.assertEqual(len(second.context["page"]), 7)
        self.assertFalse(
            {p.pk for p in first.context["page"]} & {p.pk for p in second.context["page"]}
        )
        self.assertContains(first, "mode=year")
        self.assertContains(first, "page=2")

    def test_invalid_filters_and_project_scope(self):
        for filters in (
            {},
            {"year": "abc"},
            {"year": 0},
            {"year": 10000},
            {"year": 2020, "step": "bad"},
            {"year": 2020, "mode": "bad"},
        ):
            self.assertEqual(self.client.get(self.url, filters).status_code, 400)
        self.assertEqual(
            self.client.get("/project/999999/plots/entries/?year=2020").status_code, 404
        )
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_trace_filters_hover_and_async_ready_event(self):
        data = stage_year_cumulative(self.project)
        for build, mode in (
            (build_yearly_figure, "year"),
            (build_stage_cumulative_figure, "through"),
        ):
            with patch(
                "plotly.graph_objects.Figure.to_html", autospec=True, return_value="plot"
            ) as render:
                build(data)
            figure = render.call_args.args[0]
            for trace, step in zip(figure.data, ("found", "title", "abstract")):
                self.assertEqual(
                    list(trace.customdata), [[step, mode, year] for year in data["years"]]
                )
                self.assertIn("%{y}", trace.hovertemplate)
            self.assertIn("survey-chart-ready", render.call_args.kwargs["post_script"])

    def test_dialog_on_both_chart_pages(self):
        for url in (
            reverse("project_detail", args=[self.project.pk]),
            reverse("plots_view", args=[self.project.pk]),
        ):
            response = self.client.get(url)
            self.assertContains(response, 'id="chart-paper-popup"')
            self.assertContains(response, "chart_popup.js")
            self.assertContains(response, self.url)
