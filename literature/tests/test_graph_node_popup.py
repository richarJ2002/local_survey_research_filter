"""Verify graph-node popup details, escaping, and renderer consistency.

@File:   test_graph_node_popup.py

@Brief:  Tests for the shared graph-node popup renderer metadata.

@Date:   02/10/2026
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

from literature.services.plots_service import build_citation_figure

# ------------------------------------------------------------------------ #
# Popup renderer tests
# ------------------------------------------------------------------------ #


class GraphNodePopupTests(TestCase):
    """Raw per-node details ride in meta; hover text and layout stay put."""

    # --- Draft fixture ---
    def _draft_graph(self) -> dict:
        """Build two draft nodes with one newer-to-older citation.

        Two draft papers citing backwards, newest first.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Draft graph with raw title markup, years, and a single edge.
        """
        return {
            "title": "How your kept papers link together",
            "nodes": [
                {
                    "id": 1,
                    "title": "New <b>study</b>",
                    "year": 2023,
                    "citations": 7,
                    "venue": "Nature",
                    "is_seed": True,
                },
                {
                    "id": 2,
                    "title": "Old method",
                    "year": 2020,
                    "citations": 0,
                    "venue": "",
                    "is_seed": True,
                },
            ],
            "edges": [(1, 2)],
        }

    # --- Curated fixture ---
    def _curated_graph(self) -> dict:
        """Build linked curated nodes with reference labels and a missing year.

        Survey snapshots carry project-unique ref IDs.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Curated graph with reference numbers three and four and
                one directed edge.
        """
        return {
            "title": "Curated survey graph",
            "curated": True,
            "nodes": [
                {
                    "id": 11,
                    "title": "Chosen entry",
                    "year": 2021,
                    "citations": 0,
                    "venue": "Science",
                    "is_seed": True,
                    "is_joiner": False,
                    "reference_number": 3,
                },
                {
                    "id": 12,
                    "title": "Second entry",
                    "year": None,
                    "citations": 0,
                    "venue": "",
                    "is_seed": True,
                    "is_joiner": False,
                    "reference_number": 4,
                },
            ],
            "edges": [(11, 12)],
        }

    # --- Raw details travel with the points ---
    def test_meta_carries_raw_details_for_draft(self) -> None:
        """Include draft popup metadata, escaped hover text, and readiness.

        Meta holds raw dicts aligned to points; customdata HTML is kept.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        html = build_citation_figure(self._draft_graph())
        self.assertIn("paper_details", html)
        self.assertIn("New &lt;b&gt;study", html)  # escaped hover text kept
        self.assertIn("graph_node_popup.js", html)
        self.assertIn("graph-ready", html)

    def test_full_title_and_trace_alignment_are_safe_in_inline_html(
        self,
    ) -> None:
        """Align full titles to nodes without emitting an inline script attack.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        graph = self._draft_graph()
        title = "Full paper title " * 30 + "</script><script>alert(1)</script>"
        graph["nodes"][0]["title"] = title
        with patch(
            "plotly.graph_objects.Figure.to_html", autospec=True,
            return_value="plot",
        ) as render:
            build_citation_figure(graph)
        figure = render.call_args.args[0]
        node_trace = next(trace for trace in figure.data if trace.meta)
        details = node_trace.meta["paper_details"]
        self.assertEqual(details[0]["title"], title)
        self.assertEqual(details[1]["title"], "Old method")
        self.assertEqual(len(details), len(node_trace.x))
        self.assertNotIn("</script><script>alert(1)</script>",
                         build_citation_figure(graph))

    # --- Curated graph shares the renderer ---
    def test_meta_carries_ref_ids_for_curated(self) -> None:
        """Render curated reference labels with popup metadata and readiness.

        Curated ref IDs appear in meta and in the circle labels.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        html = build_citation_figure(self._curated_graph())
        self.assertIn("paper_details", html)
        self.assertIn("[3]", html)
        self.assertIn("[4]", html)
        self.assertIn("graph_node_popup.js", html)
        self.assertIn("graph-ready", html)
        self.assertIn("Your chosen survey entries", html)

    # --- Existing chrome untouched ---
    def test_legends_hover_and_layout_preserved(self) -> None:
        """Keep legends, citation tooltips, width guidance, and native hover.

        Colours, edge tooltips, year labels and width survive the popup.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        for graph in (self._draft_graph(), self._curated_graph()):
            html = build_citation_figure(graph)
            self.assertIn("Your chosen survey entries" if graph.get("curated")
                          else "Your kept papers", html)
            self.assertIn("Dot colour", html)
            self.assertIn("cites", html)
            self.assertIn("Citation direction", html)
            self.assertIn("scroll horizontally", html)
            self.assertIn("closest", html)  # native hovermode kept as fallback

    # --- Full colour key when every role is present ---
    def test_all_three_roles_keep_colour_key(self) -> None:
        """Retain all three role legends for seeds, joiners, and branches.

        Seed, joiner and outer traces each keep their legend entry.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        html = build_citation_figure(
            {
                "title": "Mixed",
                "nodes": [
                    {
                        "id": 1,
                        "title": "Seed",
                        "year": 2023,
                        "citations": 1,
                        "venue": "V",
                        "is_seed": True,
                    },
                    {
                        "id": 2,
                        "title": "Joiner",
                        "year": 2021,
                        "citations": 1,
                        "venue": "V",
                        "is_joiner": True,
                    },
                    {"id": 3, "title": "Outer", "year": 2019,
                        "citations": 0, "venue": ""},
                ],
                "edges": [(1, 2), (2, 3)],
            }
        )
        self.assertIn("Your kept papers", html)
        self.assertIn("Links between kept papers", html)
        self.assertIn("Unconnected branches", html)

    # --- Empty graph stays ordinary ---
    def test_no_node_case_stays_ordinary_empty_graph(self) -> None:
        """Render an empty graph without popup metadata, script, or readiness.

        No papers means no popup metadata, readiness or script.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        html = build_citation_figure({"nodes": [], "edges": []})
        self.assertNotIn("no papers to show yet", html)
        self.assertIn('"xaxis":{"visible":false,"fixedrange":true}', html)
        self.assertIn('"yaxis":{"visible":false,"fixedrange":true}', html)
        self.assertNotIn("paper_details", html)
        self.assertNotIn("graph-ready", html)
        self.assertNotIn("graph_node_popup.js", html)

    # --- Both live builders flow through one renderer ---
    def test_draft_and_curated_builders_share_renderer(self) -> None:
        """Render popup-ready figures from draft and curated graph builders.

        Stored draft and survey entries both emit popup-ready figures.

        Args:
            None

        Kwargs:
            None

        Returns:
            None
        """
        from literature.models import (
            Discovery, Paper, Project, Search, SurveyEntry,
        )

        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(
            project=project, name="S", keywords=["a"])
        paper = Paper.objects.create(
            title="Draft paper",
            publication_year=2022,
            venue=" deep",
            citation_count=2,
            openalex_id="WG1",
            source_ids={"openalex": "WG1"},
            references=[],
        )
        Discovery.objects.create(
            paper=paper, search=search, source="openalex", query="a")
        entry = SurveyEntry.objects.create(
            project=project,
            reference_number=1,
            title="Curated entry",
            publication_year=2021,
            venue="Science",
        )
        from literature.services.citation_service import build_graph
        from literature.services.survey_graph_service import build_survey_graph

        for graph in (
            build_graph(project, seed_stage="found"),
            build_survey_graph(project),
        ):
            html = build_citation_figure(graph)
            self.assertIn("paper_details", html)
            self.assertIn("graph-ready", html)
            self.assertIn("graph_node_popup.js", html)
        self.assertTrue(entry.pk and paper.pk)
