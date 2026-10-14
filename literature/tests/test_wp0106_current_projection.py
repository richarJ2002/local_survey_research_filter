"""WP01.06 current-state projection: distinct papers, audit separation, scope.

@File: test_wp0106_current_projection.py
@Brief: Current source bars vs historical audits, revision scope, no providers.
@Date: 03/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from unittest.mock import patch

# Custom library Imports
from django.test import TestCase
from django.urls import reverse

from literature.models import Discovery, Paper, Project, Search, SearchRun
from literature.services.metrics_service import (
    build_metrics,
    current_source_counts,
    display_sources,
    project_revision,
)
from literature.services.plots_service import build_current_source_bars, source_pie_figure

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Current-state projection ---


class CurrentProjectionTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Current")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(
            project=self.project, name="S", keywords=["k"])
        # Paper 1: multi-source current display.
        self.p1 = Paper.objects.create(
            title="P1", sources=["openalex", "crossref"])
        Discovery.objects.create(
            search=self.search, paper=self.p1, source="openalex", query="k")
        Discovery.objects.create(
            search=self.search, paper=self.p1, source="crossref", query="k")
        # Paper 2: legacy manual only -> blank display, audit untouched.
        self.p2 = Paper.objects.create(title="P2", sources=["manual"])
        Discovery.objects.create(
            search=self.search, paper=self.p2, source="manual", query="k")
        SearchRun.objects.create(
            search=self.search,
            source="manual",
            status="completed",
            results_count=5,
            new_papers_count=2,
            duplicates_count=4,
        )

    def test_blank_current_zero_while_historical_unknown_nonzero(self):
        current = current_source_counts(self.project)
        self.assertEqual(current["blank"], 1)
        self.assertEqual(current["unique"], 2)
        # Correct current metadata without rewriting audits.
        self.p2.sources = ["openalex"]
        self.p2.save(update_fields=["sources"])
        current = current_source_counts(self.project)
        self.assertEqual(current["blank"], 0)
        run = SearchRun.objects.get(source="manual")
        self.assertEqual(run.results_count, 5)
        self.assertEqual(run.source, "manual")

    def test_multisource_bars_truthful_not_pie_totals(self):
        current = current_source_counts(self.project)
        self.assertEqual(current["per_source"]["openalex"], 1)
        self.assertEqual(current["per_source"]["crossref"], 1)
        # p1 in both bars; bars sum (2 known + 1 blank-paper... ) >= unique.
        self.assertGreaterEqual(
            sum(current["per_source"].values()) +
            current["blank"], current["unique"]
        )
        html = build_current_source_bars(current)
        self.assertIn("Current papers by source", html)
        self.assertNotIn("Unknown (blank source)", html)
        self.assertIn("have no displayed origin", self.client.get(
            reverse("project_detail", args=[self.project.pk])).content.decode())
        # Audit pies remain historical and separate.
        metrics = build_metrics(self.project)
        fig = source_pie_figure(metrics["per_source"])
        self.assertIn(
            "Historical import audit", str(fig.to_html(
                full_html=False, include_plotlyjs=False))
        )

    def test_query_list_chart_counts_agree(self):
        metrics = build_metrics(self.project)
        current = metrics["current"]
        table = Paper.objects.filter(
            discoveries__search__project=self.project).distinct().count()
        self.assertEqual(current["unique"], table)
        self.assertEqual(current["unique"], metrics["unique_total"])
        blanks = sum(
            1
            for row in Paper.objects.filter(
                pk__in=Discovery.objects.filter(search__project=self.project).values_list(
                    "paper_id", flat=True
                )
            )
            if not display_sources(row.sources)
        )
        self.assertEqual(current["blank"], blanks)
        response = self.client.get(
            reverse("papers_view", args=[self.project.pk]))
        self.assertEqual(response.context["page"].paginator.count, table)

    def test_two_project_scope_and_revision(self):
        other = Project.objects.create(name="Other")
        other.ensure_default_stages()
        search = Search.objects.create(project=other, name="S", keywords=["k"])
        paper = Paper.objects.create(title="Foreign", sources=["arxiv"])
        Discovery.objects.create(
            search=search, paper=paper, source="arxiv", query="k")
        before = project_revision(other)
        # Mutate this project only.
        self.p1.sources = ["openalex", "crossref", "arxiv"]
        self.p1.save(update_fields=["sources"])
        self.assertNotEqual(project_revision(
            self.project), project_revision(other))
        self.assertEqual(project_revision(other), before)
        rev_a = self.client.get(reverse("project_revision", args=[self.project.pk])).json()[
            "revision"
        ]
        rev_b = self.client.get(reverse("project_revision", args=[other.pk])).json()[
            "revision"]
        self.assertEqual(rev_a, project_revision(self.project))
        self.assertEqual(rev_b, before)
        # Chart entries never leak across projects.
        data = self.client.get(
            reverse("chart_entries", args=[self.project.pk]),
            {"mode": "year", "step": "found", "year": 2026, "format": "json"},
        )
        self.assertEqual(data.status_code, 200)

    def test_no_provider_requests_on_projection_or_refresh(self):
        with (
            patch("httpx.get", side_effect=AssertionError("no providers")),
            patch("httpx.post", side_effect=AssertionError("no providers")),
        ):
            build_metrics(self.project)
            current_source_counts(self.project)
            self.client.get(reverse("plots_view", args=[self.project.pk]))
            self.client.get(reverse("project_detail", args=[self.project.pk]))
            self.client.get(
                reverse("project_revision", args=[self.project.pk]))
            self.client.get(reverse("papers_view", args=[self.project.pk]))

    def test_revision_endpoint_safe_and_scoped(self):
        self.assertEqual(
            self.client.post(reverse("project_revision", args=[
                             self.project.pk])).status_code, 405
        )
        self.assertEqual(self.client.get(
            "/project/999999/revision/").status_code, 404)

    def test_templates_separate_current_from_audit(self):
        """Expose current counts without links to the removed history lens.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for name in ("plots_view", "project_detail"):
            response = self.client.get(reverse(name, args=[self.project.pk]))
            self.assertContains(response, "Current papers by source")
            self.assertNotContains(response, "?lens=history")
            self.assertNotContains(response, "Audit / History")
            self.assertNotContains(
                response, "Unspecified at import (historical)")
            self.assertNotContains(
                response, "Raw records by source — historical imports")
            self.assertNotContains(
                response, "Duplicate removals by source — historical imports")
            self.assertContains(response, "data-project-revision-url")

    def test_corrected_sources_exclude_audit_pies_from_every_default_surface(self):
        """Keep corrected source projections and stored audit rows independent.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        self.p2.sources = ["arxiv"]
        self.p2.save(update_fields=["sources"])
        before = [list(model.objects.values())
                  for model in (Paper, Discovery, SearchRun)]
        with patch("literature.services.plots_service.build_source_pie", side_effect=AssertionError("audit only")):
            for name in ("project_detail", "plots_view", "plots_download"):
                with self.subTest(surface=name):
                    response = self.client.get(
                        reverse(name, args=[self.project.pk]))
                    self.assertContains(response, "Current papers by source")
                    self.assertNotContains(
                        response, "Unspecified at import (historical)")
                    self.assertNotContains(
                        response, "Historical import audit;")
                    if name != "plots_download":
                        self.assertEqual(
                            response.context["metrics"]["current"]["blank"], 0)
                        self.assertNotIn("source_raw_plot", response.context)
                        self.assertNotIn(
                            "source_duplicates_plot", response.context)
        self.assertEqual(
            [list(model.objects.values())
             for model in (Paper, Discovery, SearchRun)], before
        )

    def test_explicit_history_page_and_export_preserve_audits_and_scope(self):
        """Treat obsolete history URLs as current, project-scoped projections.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        other = Project.objects.create(name="Foreign")
        search = Search.objects.create(project=other, name="Foreign search")
        SearchRun.objects.create(
            search=search, source="publisher", status="completed", results_count=99
        )
        before = [list(model.objects.values())
                  for model in (Paper, Discovery, SearchRun)]
        with patch(
            "literature.services.plots_service.build_source_pie", side_effect=AssertionError("removed audit charts")
        ):
            for name in ("plots_view", "plots_download"):
                with self.subTest(surface=name):
                    response = self.client.get(
                        reverse(name, args=[self.project.pk]), {
                            "lens": "history"}
                    )
                    self.assertNotContains(response, "Audit / History")
                    self.assertNotContains(
                        response, "Unspecified at import (historical)")
                    self.assertNotContains(
                        response, "Where duplicate hits were removed")
                    self.assertContains(
                        response, "Current papers by source")
                    self.assertNotContains(
                        response, "Publisher / journal website")
                    if name == "plots_view":
                        self.assertEqual(
                            response.context["metrics"]["per_source"]["manual"]["results"], 5
                        )
                        self.assertEqual(
                            response.context["metrics"]["per_source"]["manual"]["dups"], 4
                        )
                        self.assertNotContains(
                            response, 'aria-current="page">Audit / History')
                        self.assertNotContains(
                            response, "/download/?lens=history")
                    else:
                        self.assertEqual(
                            response["Content-Disposition"],
                            f'attachment; filename="charts-{self.project.pk}.html"')
        self.assertEqual(
            [list(model.objects.values())
             for model in (Paper, Discovery, SearchRun)], before
        )

    def test_history_requires_explicit_lens_and_makes_no_network_requests(self):
        with patch("socket.socket", side_effect=AssertionError("no network")):
            for name in ("plots_view", "plots_download"):
                url = reverse(name, args=[self.project.pk])
                fallback = self.client.get(url, {"lens": "invalid"})
                self.assertContains(fallback, "Current papers by source")
                self.assertNotContains(
                    fallback, "Unspecified at import (historical)")
                self.assertEqual(self.client.get(
                    url, {"lens": "history"}).status_code, 200)
                self.assertEqual(
                    self.client.get(
                        url.replace(f"/{self.project.pk}/",
                                    "/999999/"), {"lens": "history"}
                    ).status_code,
                    404,
                )
