#!/usr/bin/env python3

'''
@File:   test_plots.py

@Brief:  Tests for keyword counts and dashboard figures.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.models import Discovery, Paper, Project, ScreeningDecision, Search
from literature.services.metrics_service import keyword_stage_counts
from literature.services.plots_service import (
    build_funnel_figure,
    build_keyword_figure,
    build_source_pie,
    build_yearly_figure,
    source_pie_figure,
)

# ------------------------------------------------------------------------ #
# Plot tests
# ------------------------------------------------------------------------ #


class PlotTests(TestCase):
    """Counts attribute papers to keywords; figures carry plain titles."""

    # --- Setup helper ---
    def _setup(self) -> Project:
        """Create project with two papers and one accepted."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["battery", "solar"])
        p1 = Paper.objects.create(title="Battery study", abstract="x", publication_year=2020)
        p2 = Paper.objects.create(title="Solar panel", abstract="y", publication_year=2021)
        Discovery.objects.create(paper=p1, search=search, source="openalex", query="battery")
        Discovery.objects.create(paper=p2, search=search, source="openalex", query="solar")
        title = project.stages.get(name="Title")
        ScreeningDecision.objects.create(paper=p1, stage=title, decision="ACCEPT")
        ScreeningDecision.objects.create(paper=p2, stage=title, decision="REJECT")
        return project

    # --- Counts ---
    def test_keyword_counts_split_stages(self) -> None:
        """Battery row hits its paper through title check."""
        project = self._setup()
        counts = keyword_stage_counts(project)
        self.assertEqual(counts["battery"]["found"], 1)
        self.assertEqual(counts["battery"]["title"], 1)
        self.assertEqual(counts["solar"]["title"], 0)

    # --- Figures explain themselves ---
    def test_figures_have_plain_titles(self) -> None:
        """Every figure embeds a plain-English title."""
        project = self._setup()
        counts = keyword_stage_counts(project)
        self.assertIn("How many papers each keyword found", build_keyword_figure(counts))
        self.assertIn(
            "Where the found records came from",
            build_source_pie({"openalex": {"results": 2, "new": 2, "dups": 0, "runs": 1}}),
        )
        self.assertIn("Where duplicate hits were removed", build_source_pie({}, metric="dups"))
        self.assertIn("Where incomplete papers were excluded", build_source_pie({}, metric="incomplete"))
        from literature.services.metrics_service import build_metrics

        metrics = build_metrics(project)
        funnel_html = build_funnel_figure(metrics)
        self.assertIn("How papers flow", funnel_html)
        self.assertIn("Abstract: kept", funnel_html)
        self.assertNotIn("Incomplete", funnel_html)
        from literature.services.metrics_service import stage_year_cumulative

        stage_data = stage_year_cumulative(project)
        self.assertIn("Kept after title check", build_yearly_figure(stage_data))

    def test_source_pies_show_outcome_share_and_within_source_rate(self) -> None:
        """Percent-of-category slices also explain unequal source volumes."""
        sources = {
            "openalex": {"results": 100, "dups": 10, "incomplete": 5},
            "semantic_scholar": {"results": 10, "dups": 5, "incomplete": 0},
        }
        raw = source_pie_figure(sources)
        duplicates = source_pie_figure(sources, metric="dups")
        incomplete = source_pie_figure(sources, metric="incomplete")
        self.assertEqual(list(raw.data[0].values), [100, 10])
        self.assertEqual(list(duplicates.data[0].values), [10, 5])
        self.assertEqual(list(duplicates.data[0].customdata), [10.0, 50.0])
        self.assertEqual(list(incomplete.data[0].values), [5, 0])
        self.assertEqual(list(incomplete.data[0].customdata), [5.0, 0.0])
        self.assertIn("Share of this outcome", duplicates.data[0].hovertemplate)
        self.assertIn("Of this source's raw records", incomplete.data[0].hovertemplate)

        empty = source_pie_figure({"openalex": {"results": 4, "dups": 0}}, metric="dups")
        self.assertEqual(len(empty.data), 0)
        self.assertIn("No records in this category yet", empty.layout.annotations[0].text)

    # --- Track table ---
    def test_tracks_table(self) -> None:
        """Linked papers produce a track row with keywords."""
        from literature.services.citation_service import build_graph, track_summaries

        project = self._setup()
        project.ensure_default_stages()
        from literature.models import Discovery, Paper

        a = Paper.objects.create(
            title="A", openalex_id="WA9", source_ids={"openalex": "WA9"},
            references=["https://openalex.org/WB9"], citation_count=5,
            publication_year=2022, keywords=["robotics"], venue="Nature",
        )
        b = Paper.objects.create(
            title="B", openalex_id="WB9", source_ids={"openalex": "WB9"},
            references=[], citation_count=10,
            publication_year=2020, keywords=["robotics"], venue="Science",
        )
        from literature.models import Search

        search = Search.objects.create(project=project, name="S2", keywords=["robotics"])
        for paper in (a, b):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="robotics")
        abstract = project.stages.get(name="Abstract")
        ScreeningDecision.objects.create(paper=a, stage=abstract, decision="ACCEPT")
        graph = build_graph(project, depth=2)
        tracks = track_summaries(project, graph)
        self.assertEqual(len(tracks), 1)
        self.assertIn("robotics", tracks[0]["top_keywords"])

    # --- Dashboard loads ---
    def test_plots_view_loads(self) -> None:
        """Plots page renders with charts."""
        project = self._setup()
        resp = self.client.get(f"/project/{project.pk}/plots/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Charts for", resp.content.decode())
        for key in ("source_raw_plot", "source_duplicates_plot"):
            self.assertIn("plotly-graph-div", resp.context[key])
        self.assertEqual(len({resp.context[key] for key in (
            "source_raw_plot", "source_duplicates_plot",
        )}), 2)
        self.assertNotIn("source_incomplete_plot", resp.context)
        download = self.client.get(f"/project/{project.pk}/plots/download/")
        self.assertContains(download, "Where the found records came from")
        self.assertContains(download, "Where duplicate hits were removed")
        self.assertNotContains(download, "Where incomplete papers were excluded")

    def test_citation_colour_key_explains_circles(self) -> None:
        """Both citation cards key green/grey/blue circles in plain words."""
        project = self._setup()
        for url in (f"/project/{project.pk}/", f"/project/{project.pk}/plots/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            content = response.content.decode()
            self.assertIn("Circle colour key", content)
            self.assertIn("your selected (kept) papers", content)
            self.assertIn("direct-citation path joining selected papers", content)
            self.assertIn("branches that do not connect selected papers", content)

    def test_track_summary_section_is_not_shown(self) -> None:
        """Keep the citation graph, not the keyword-based track table."""
        project = self._setup()
        for url in (f"/project/{project.pk}/", f"/project/{project.pk}/plots/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "How your kept papers link together")
            self.assertNotContains(response, "What each track is about")

    def test_shortest_path_checkbox_renders_checked_and_explained(self) -> None:
        """The existing filter can be activated from the charts page."""
        project = self._setup()
        resp = self.client.get(f"/project/{project.pk}/plots/?prune=on")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["prune"])
        self.assertContains(resp, 'name="prune" checked')
        self.assertContains(resp, "Show shortest-path backbone")
        self.assertContains(resp, "Steps back through citations")
        self.assertContains(resp, "Start from your selected papers, newest first")
        self.assertContains(resp, "prune=on")

    def test_download_matches_filtered_citation_graph(self) -> None:
        """The charts download honors the checked graph filter from the page."""
        project = self._setup()
        search = project.searches.first()
        kept = Paper.objects.create(
            title="Kept", openalex_id="WD1", references=["https://openalex.org/WD2"],
        )
        branch = Paper.objects.create(title="Unique branch paper", openalex_id="WD2")
        for paper in (kept, branch):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        ScreeningDecision.objects.create(paper=kept, stage=abstract, decision="ACCEPT")
        unfiltered = self.client.get(f"/project/{project.pk}/plots/download/")
        filtered = self.client.get(
            f"/project/{project.pk}/plots/download/?prune=on&stage=final&depth=3"
        )
        self.assertIn("Unique branch paper", unfiltered.content.decode())
        self.assertNotIn("Unique branch paper", filtered.content.decode())

    def test_reference_lists_separate_final_and_visible_citation_papers(self) -> None:
        """Markdown downloads respect final decisions and graph depth."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        final = Paper.objects.create(
            title="Final study", doi="10.1/final", publication_year=2024,
            authors=[{"name": "A. Author"}], venue="Journal A",
            references=["https://openalex.org/WREF"],
        )
        cited = Paper.objects.create(title="Cited method", openalex_id="WREF",
                                     publication_year=2020)
        outside = Paper.objects.create(
            title="Incoming only", openalex_id="WOUT", publication_year=2025,
            references=["https://doi.org/10.1/final"],
        )
        for paper in (final, cited, outside):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        ScreeningDecision.objects.create(
            paper=final, stage=project.stages.get(name="Abstract"), decision="ACCEPT",
        )
        final_url = f"/project/{project.pk}/references/final/download/"
        citations_url = f"/project/{project.pk}/references/citations/download/"
        final_response = self.client.get(final_url)
        cited_response = self.client.get(citations_url + "?depth=1&stage=final")
        self.assertEqual(final_response.status_code, 200)
        self.assertIn("references-final-", final_response["Content-Disposition"])
        self.assertIn("Final study", final_response.content.decode())
        self.assertIn("A. Author (2024)", final_response.content.decode())
        self.assertNotIn("Cited method", final_response.content.decode())
        self.assertIn("Cited method", cited_response.content.decode())
        self.assertNotIn("Final study", cited_response.content.decode())
        self.assertNotIn("Incoming only", cited_response.content.decode())
        self.assertIn("_No papers in this group._",
                      self.client.get(citations_url + "?depth=0").content.decode())
        self.assertEqual(self.client.get(
            f"/project/{project.pk}/references/unknown/download/"
        ).status_code, 404)
        plots = self.client.get(f"/project/{project.pk}/plots/?depth=1&prune=on")
        self.assertContains(plots, "Download final selected references")
        self.assertContains(plots, "references/citations/download/?depth=1&amp;chain=1&amp;stage=final&amp;prune=on")
