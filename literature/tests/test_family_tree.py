#!/usr/bin/env python3

'''
@File:   test_family_tree.py

@Brief:  Tests for newest-first backward citation layout.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import re

# Custom library Imports
from django.test import TestCase

from literature.services.plots_service import family_positions

# ------------------------------------------------------------------------ #
# Layout tests
# ------------------------------------------------------------------------ #


class FamilyTreeTests(TestCase):
    """Citation clusters start at the newest selected paper and read back."""

    # --- Roots and rows ---
    def test_newest_is_root_and_rows_strict(self) -> None:
        """Newest paper flagged root; same-year dots never coincide."""
        nodes = [
            {"id": 1, "title": "Old", "year": 2018, "citations": 1, "venue": ""},
            {"id": 2, "title": "Mid", "year": 2020, "citations": 4, "venue": ""},
            {"id": 3, "title": "New", "year": 2023, "citations": 2, "venue": ""},
            {"id": 4, "title": "Twin", "year": 2020, "citations": 0, "venue": ""},
        ]
        edges = [(3, 2), (2, 1), (4, 2)]
        positions, roots = family_positions(nodes, edges)
        self.assertEqual(roots, {3})
        # Evenly spaced year ranks, newest on top (not calendar ticks).
        self.assertGreater(positions[3][1], positions[1][1])
        # No two dots share a spot.
        self.assertEqual(len(set(positions.values())), 4)
        # Same-year dots have room for markers and labels.
        self.assertEqual(abs(positions[2][0] - positions[4][0]), 2.5)

    def test_barycentric_layout_uncrosses_parallel_citation_lines(self) -> None:
        """Linked descendants should align with their cited papers."""
        nodes = [
            {"id": 1, "year": 2023}, {"id": 2, "year": 2023},
            {"id": 3, "year": 2020}, {"id": 4, "year": 2020},
        ]
        positions, _ = family_positions(nodes, [(1, 4), (2, 3)])
        self.assertLess(positions[1][0], positions[2][0])
        self.assertLess(positions[4][0], positions[3][0])
        self.assertEqual(positions, family_positions(nodes, [(1, 4), (2, 3)])[0])

    def test_newest_selected_paper_starts_cluster(self) -> None:
        """An unselected later paper cannot replace a selected starting point."""
        nodes = [
            {"id": 1, "year": 2022, "is_seed": True},
            {"id": 2, "year": 2020, "is_seed": True},
            {"id": 3, "year": 2025, "is_seed": False},
        ]
        _, roots = family_positions(nodes, [(1, 2), (3, 2)])
        self.assertEqual(roots, {1})

    def test_disconnected_clusters_have_separate_horizontal_lanes(self) -> None:
        """A second citation tree does not overlap the first at the same year."""
        nodes = [{"id": pk, "year": year} for pk, year in
                 ((1, 2020), (2, 2021), (3, 2020), (4, 2021))]
        positions, _ = family_positions(nodes, [(2, 1), (4, 3)])
        self.assertGreater(min(positions[pk][0] for pk in (3, 4)),
                           max(positions[pk][0] for pk in (1, 2)))

    def test_wide_cluster_renders_in_scrollable_plot(self) -> None:
        """Same-year nodes get more real pixels instead of shrinking to fit."""
        from literature.services.plots_service import build_citation_figure

        nodes = [
            {"id": pk, "year": 2020, "title": f"Paper {pk}",
             "citations": 0, "venue": "", "is_seed": True}
            for pk in range(12)
        ]
        html = build_citation_figure({"nodes": nodes,
                                      "edges": [(pk, pk + 1) for pk in range(11)]})
        self.assertIn('aria-label="Citation graph, scroll horizontally"', html)
        self.assertIn('overflow-x:auto', html)
        widths = re.findall(r'"width":(\d+)', html)
        self.assertTrue(widths)
        self.assertGreater(max(map(int, widths)), 1100)
        self.assertIn('"text":["2020"', html)
        self.assertIn('"yaxis":{"visible":false,"fixedrange":false}', html)
