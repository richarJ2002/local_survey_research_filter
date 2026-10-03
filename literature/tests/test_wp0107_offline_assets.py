"""WP01.07 offline local assets: local Plotly.js, self-contained exports, offline work.

@File: test_wp0107_offline_assets.py
@Brief: No CDN paths, local bundle serving, standalone exports, offline pages.
@Date: 03/10/2026
"""

import socket
from unittest.mock import patch

import plotly
from django.test import TestCase, override_settings
from django.urls import resolve, reverse

from literature.models import Discovery, OpenCitationsLookup, Paper, Project, Search
from literature.plotly_assets import (
    THIRD_PARTY_NOTICE,
    plotly_bundle,
    plotly_version,
)
from literature.services.citation_service import build_graph
from literature.services.metrics_service import build_metrics, stage_year_cumulative
from literature.services.plots_service import (
    build_citation_figure,
    build_current_source_bars,
    build_funnel_figure,
    build_keyword_figure,
    build_source_pie,
    build_stage_cumulative_figure,
    build_yearly_figure,
)
from literature.worker import is_research_path


# --- Installed bundle served locally ---
class LocalBundleTests(TestCase):
    def test_version_matches_installed_dependency(self):
        self.assertEqual(plotly_version(), plotly.__version__)

    def test_bundle_is_the_installed_plotly_js(self):
        bundle = plotly_bundle()
        self.assertGreater(len(bundle), 1_000_000)
        self.assertTrue(bundle.lstrip().startswith(b"/**"))

    def test_asset_endpoint_serves_bundle_with_version_and_cache(self):
        response = self.client.get(reverse("plotly_js"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertEqual(response["X-Plotly-Version"], plotly.__version__)
        self.assertIn("immutable", response["Cache-Control"])
        self.assertEqual(response.content, plotly_bundle())

    def test_asset_route_is_worker_servable_research_path(self):
        self.assertEqual(resolve(reverse("plotly_js")).view_name, "plotly_js")
        self.assertTrue(is_research_path("project/plotly.min.js"))


# --- No required CDN anywhere in figures or pages ---
class NoCdnTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Offline")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(project=self.project, name="S", keywords=["k"])
        self.paper = Paper.objects.create(
            title="Offline Paper", sources=["openalex"], publication_year=2024,
        )
        Discovery.objects.create(search=self.search, paper=self.paper, source="openalex")

    def _figures(self):
        metrics = build_metrics(self.project)
        stage_data = stage_year_cumulative(self.project)
        return [
            build_keyword_figure({"k": {"found": 1, "title": 1, "abstract": 0}}),
            build_current_source_bars(metrics.get("current", {})),
            build_source_pie(metrics.get("per_source", {})),
            build_source_pie(metrics.get("per_source", {}), metric="dups"),
            build_funnel_figure(metrics),
            build_yearly_figure(stage_data),
            build_yearly_figure([2024, 2023]),
            build_stage_cumulative_figure(stage_data),
            build_stage_cumulative_figure({}),
            build_citation_figure(build_graph(self.project)),
            build_citation_figure({"nodes": [], "edges": []}),
        ]

    def test_figures_have_no_cdn_or_remote_urls(self):
        for html in self._figures():
            text = str(html)
            self.assertIn("plotly-graph-div", text)
            self.assertNotIn("cdn", text.lower())
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)

    def test_pages_reference_local_bundle_not_cdn(self):
        for name in ("plots_view", "project_detail", "survey_graph"):
            response = self.client.get(reverse(name, args=[self.project.pk]))
            self.assertEqual(response.status_code, 200, name)
            content = response.content.decode()
            self.assertIn(reverse("plotly_js"), content, name)
            self.assertNotIn("cdn.plot.ly", content, name)
            self.assertNotIn("cdn.jsdelivr", content, name)
            self.assertNotIn("unpkg.com", content, name)


# --- Standalone export is self-contained and secret-free ---
class StandaloneExportTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Export")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(project=self.project, name="S", keywords=["k"])
        paper = Paper.objects.create(title="Export Paper", sources=["arxiv"])
        Discovery.objects.create(search=self.search, paper=paper, source="arxiv")

    def test_download_needs_no_cdn_or_app_server(self):
        response = self.client.get(reverse("plots_download", args=[self.project.pk]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("plotly.js v", content)
        self.assertIn("MIT", content)
        self.assertIn(THIRD_PARTY_NOTICE.split(" (")[0], content)
        self.assertIn("need the running app", content)
        # ponytail: the bundle carries default-URL strings (e.g. topojson),
        # but our own markup must reference no remote resource at all.
        markup = content.replace(plotly_bundle().decode("utf-8"), "")
        self.assertNotIn('src="http://', markup)
        self.assertNotIn('src="https://', markup)
        self.assertNotIn('href="http://', markup)
        self.assertNotIn('href="https://', markup)
        self.assertNotIn("graph_node_popup.js", markup)
        self.assertIn("plotly-graph-div", markup)

    def test_download_embeds_no_session_or_csrf_secrets(self):
        self.client.get(reverse("plots_view", args=[self.project.pk]))
        session_cookie = self.client.cookies.get("sessionid")
        csrf_cookie = self.client.cookies.get("csrftoken")
        content = self.client.get(
            reverse("plots_download", args=[self.project.pk])).content.decode()
        for secret in (session_cookie, csrf_cookie):
            if secret is not None and secret.value:
                self.assertNotIn(secret.value, content)


# --- Local work with the network disabled; provider actions fail harmlessly ---
class OfflineLocalWorkTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="OfflineWork")
        self.project.ensure_default_stages()
        self.search = Search.objects.create(project=self.project, name="S", keywords=["k"])
        self.paper = Paper.objects.create(
            title="Keep Me", abstract="An abstract.",
            doi="10.1000/offline-test", sources=["crossref"],
        )
        Discovery.objects.create(
            search=self.search, paper=self.paper, source="crossref", query="k")

    def _offline(self):
        def _blocked(*args, **kwargs):
            raise OSError("internet disabled for WP01.07")
        return patch.object(socket, "socket", side_effect=_blocked)

    def test_browse_edit_screen_curate_graphs_work_offline(self):
        before = (
            Paper.objects.count(), Discovery.objects.count(),
            OpenCitationsLookup.objects.count(),
        )
        with self._offline():
            urls = [
                reverse("project_detail", args=[self.project.pk]),
                reverse("papers_view", args=[self.project.pk]),
                reverse("screening_view", args=[self.project.pk, "title"]),
                reverse("reference_review", args=[self.project.pk]),
                reverse("survey_graph", args=[self.project.pk]),
                reverse("plots_view", args=[self.project.pk]),
                reverse("plots_download", args=[self.project.pk]),
                reverse("project_revision", args=[self.project.pk]),
                reverse("settings_view"),
            ]
            for url in urls:
                response = self.client.get(url)
                self.assertIn(response.status_code, (200, 302), url)
            response = self.client.get(
                reverse("chart_entries", args=[self.project.pk]),
                {"mode": "year", "step": "found", "year": 2026, "format": "json"},
            )
            self.assertEqual(response.status_code, 200)
        after = (
            Paper.objects.count(), Discovery.objects.count(),
            OpenCitationsLookup.objects.count(),
        )
        self.assertEqual(before, after)

    def test_explicit_provider_posts_fail_harmlessly_offline(self):
        with self._offline():
            backfill = self.client.post(reverse("refs_backfill", args=[self.project.pk]))
            enrich = self.client.post(reverse("citations_enrich", args=[self.project.pk]))
        self.assertIn(backfill.status_code, (200, 302))
        self.assertIn(enrich.status_code, (200, 302))
        self.paper.refresh_from_db()
        self.assertEqual(self.paper.title, "Keep Me")
        self.assertEqual(self.paper.abstract, "An abstract.")
        self.assertEqual(OpenCitationsLookup.objects.count(), 0)
        messages = list(enrich.wsgi_request._messages)
        self.assertTrue(
            any("failed" in str(message).lower() or "retry" in str(message).lower()
                for message in messages),
            "offline enrichment must say the lookup failed",
        )

    @override_settings(
        OPENALEX_EMAIL="synthetic@example.com",
        OPENCITATIONS_TOKEN="synthetic-oc-token-9f8e7d6c5b4a",
    )
    def test_settings_works_offline_without_printing_token(self):
        with self._offline():
            response = self.client.get(reverse("settings_view"))
        self.assertEqual(response.status_code, 200)
        # ponytail: synthetic override only; the real .env is never read here.
        self.assertNotContains(response, "synthetic-oc-token-9f8e7d6c5b4a")
