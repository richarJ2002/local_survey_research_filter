"""Readability-first citation layout and responsive rendering.

@File: test_family_tree.py
@Brief: Geometry, soft chronology, deterministic bounded layout and preserved edges.
@Date: 02/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
"""

import re
from itertools import combinations
from math import hypot, isfinite
from unittest.mock import patch

from django.test import SimpleTestCase

from literature.services.plots_service import (
    _repair_positions,
    _spaced_positions,
    build_citation_figure,
    family_positions,
)


# --- Independently check visible geometry, not incidental coordinates ---
def crossings(points, edges):
    def side(a, b, c):
        x, y = points[a]
        bx, by = points[b]
        cx, cy = points[c]
        return (bx - x) * (cy - y) - (by - y) * (cx - x)

    return sum(
        len({a, b, c, d}) == 4
        and side(a, b, c) * side(a, b, d) < -1e-9
        and side(c, d, a) * side(c, d, b) < -1e-9
        for (a, b), (c, d) in combinations(edges, 2)
    )


class FamilyTreeTests(SimpleTestCase):
    def assert_spaced(self, points):
        self.assertTrue(all(isfinite(value) for point in points.values() for value in point))
        for a, b in combinations(points.values(), 2):
            self.assertGreaterEqual(hypot(a[0] - b[0], a[1] - b[1]), 2.5 - 1e-7)

    def test_topology_not_year_rows_and_starting_papers_preserved(self):
        nodes = [
            {"id": 1, "year": 2018},
            {"id": 2, "year": 2020},
            {"id": 3, "year": 2023},
            {"id": 4, "year": 2020},
        ]
        edges = [(3, 2), (2, 1), (4, 2)]
        points, roots = family_positions(nodes, edges)
        self.assertEqual(roots, {3})
        self.assertEqual(set(points), {1, 2, 3, 4})
        self.assert_spaced(points)
        self.assertEqual(crossings(points, edges), 0)
        self.assertNotAlmostEqual(points[2][1], points[4][1])

    def test_same_year_planar_graph_is_not_collapsed_to_a_line(self):
        nodes = [{"id": pk, "year": 2020} for pk in range(4)]
        edges = list(combinations(range(4), 2))  # K4: planar, but not on a single row.
        points, _ = family_positions(nodes, edges)
        self.assertEqual(crossings(points, edges), 0)
        self.assert_spaced(points)
        self.assertGreater(len({round(y, 6) for x, y in points.values()}), 2)

    def test_chronology_changes_orientation_only_not_geometry(self):
        edges = [(1, 2), (2, 3), (3, 1), (3, 4)]
        unknown = [{"id": pk, "year": None} for pk in range(1, 5)]
        dated = [{"id": pk, "year": 2010 + pk} for pk in range(1, 5)]
        before, _ = family_positions(unknown, edges)
        after, _ = family_positions(dated, edges)
        for a, b in combinations(before, 2):
            self.assertAlmostEqual(
                hypot(before[a][0] - before[b][0], before[a][1] - before[b][1]),
                hypot(after[a][0] - after[b][0], after[a][1] - after[b][1]),
                places=7,
            )
        mean_year = sum(node["year"] for node in dated) / len(dated)
        self.assertGreater(
            sum((node["year"] - mean_year) * after[node["id"]][1] for node in dated), 0
        )
        self.assertEqual(crossings(before, edges), crossings(after, edges))

    def test_layout_is_deterministic_and_does_not_mutate_input(self):
        nodes = [{"id": pk, "year": 2000 + pk % 4} for pk in range(12)]
        edges = [(pk, (pk + 1) % 12) for pk in range(12)] + [(0, 4), (4, 8)]
        original = [dict(node) for node in nodes]
        points, roots = family_positions(nodes, edges)
        reordered, reordered_roots = family_positions(nodes[::-1], edges[::-1])
        self.assertEqual(points, reordered)
        self.assertEqual(roots, reordered_roots)
        self.assertEqual(nodes, original)
        self.assert_spaced(points)

    def test_newest_selected_paper_remains_starting_point_not_newer_branch(self):
        nodes = [
            {"id": 1, "year": 2022, "is_seed": True},
            {"id": 2, "year": 2020, "is_seed": True},
            {"id": 3, "year": 2025, "is_seed": False},
        ]
        self.assertEqual(family_positions(nodes, [(1, 2), (3, 2)])[1], {1})

    def test_isolates_pack_in_two_dimensions_instead_of_one_long_strip(self):
        points, roots = family_positions([{"id": pk, "year": None} for pk in range(62)], [])
        self.assertEqual(len(points), 62)
        self.assertEqual(len(roots), 62)
        self.assert_spaced(points)
        width = max(x for x, y in points.values()) - min(x for x, y in points.values())
        height = max(y for x, y in points.values()) - min(y for x, y in points.values())
        self.assertGreater(height, 0)
        self.assertLess(width / height, 3)

    def test_large_connected_graph_uses_bounded_spaced_fallback(self):
        nodes = [{"id": pk, "year": None} for pk in range(260)]
        with patch(
            "literature.services.plots_service._spring_positions",
            side_effect=AssertionError("Unbounded settling"),
        ):
            points, _ = family_positions(nodes, [(pk, pk + 1) for pk in range(259)])
        self.assertEqual(len(points), 260)
        self.assert_spaced(points)
        self.assertEqual(family_positions([], []), ({}, set()))

    def test_dense_small_graph_also_uses_bounded_fallback(self):
        nodes = [{"id": pk, "year": None} for pk in range(24)]
        with patch(
            "literature.services.plots_service._spring_positions",
            side_effect=AssertionError("Dense quadratic search"),
        ):
            points, _ = family_positions(nodes, list(combinations(range(24), 2)))
        self.assert_spaced(points)
        self.assertEqual(len(points), 24)

    def test_invalid_trial_coordinates_are_skipped_not_rendered(self):
        points = {1: (0, 0), 2: (5, 5), 3: (0, 5), 4: (5, 0)}
        edges = [(1, 2), (3, 4)]
        neighbours = {1: {2}, 2: {1}, 3: {4}, 4: {3}}
        with patch(
            "literature.services.plots_service._spaced_positions",
            side_effect=ValueError("Coincident trial"),
        ):
            self.assertEqual(_repair_positions(points, edges, neighbours), points)
        for invalid in ({1: (float("nan"), 0)}, {1: (float("inf"), 0)}, {1: (0, 0), 2: (0, 0)}):
            with self.assertRaises(ValueError):
                _spaced_positions(invalid)

    def test_responsive_canvas_preserves_cycles_labels_and_every_edge(self):
        nodes = [
            {
                "id": pk,
                "year": 2020,
                "title": f"Paper {pk}",
                "citations": pk,
                "venue": "",
                "is_seed": True,
                "reference_number": pk + 1,
            }
            for pk in range(5)
        ]
        edges = list(combinations(range(5), 2)) + [(1, 0)]  # Nonplanar plus a reciprocal citation.
        with patch(
            "plotly.graph_objects.Figure.to_html", autospec=True, return_value="plot"
        ) as render:
            html = build_citation_figure({"nodes": nodes, "edges": edges})
        figure = render.call_args.args[0]
        self.assertIsNone(figure.layout.width)
        self.assertTrue(figure.layout.autosize)
        self.assertTrue(render.call_args.kwargs["config"]["responsive"])
        self.assertEqual(figure.layout.legend.orientation, "h")
        self.assertIn("width:100%;min-width:", html)
        directions = next(
            trace for trace in figure.data if trace.name == "Citation direction: citing → cited"
        )
        self.assertEqual(len(directions.x), len(edges))
        node_trace = next(trace for trace in figure.data if trace.meta)
        self.assertEqual(len(node_trace.x), 5)
        self.assertTrue(all("2020<br>[" in text for text in node_trace.text))
        self.assertEqual(
            list(node_trace.meta["paper_details"][0]),
            ["title", "year", "venue", "citations", "ref"],
        )

    def test_initial_canvas_has_buffer_outside_largest_node_and_unrelated_edge(self):
        # A node 0.8 layout units above an unrelated horizontal edge models the clearance floor.
        points = {1: (0, 0), 2: (5, 0), 3: (2.5, 0.8), 4: (-20, 35)}
        nodes = [
            {
                "id": pk,
                "year": 2020,
                "title": f"Paper {pk}",
                "venue": "",
                "citations": 100 if pk == 3 else 0,
                "is_seed": True,
            }
            for pk in points
        ]
        with (
            patch("literature.services.plots_service.family_positions", return_value=(points, {1})),
            patch(
                "plotly.graph_objects.Figure.to_html", autospec=True, return_value="plot"
            ) as render,
        ):
            html = build_citation_figure({"nodes": nodes, "edges": [(1, 2)]})
        figure = render.call_args.args[0]
        minimum_width = int(re.search(r"min-width:(\d+)px", html).group(1))
        xscale = (minimum_width - figure.layout.margin.l - figure.layout.margin.r) / (
            figure.layout.xaxis.range[1] - figure.layout.xaxis.range[0]
        )
        yscale = (figure.layout.height - figure.layout.margin.t - figure.layout.margin.b) / (
            figure.layout.yaxis.range[1] - figure.layout.yaxis.range[0]
        )
        self.assertGreaterEqual(min(xscale, yscale), 40)
        trace = next(trace for trace in figure.data if trace.meta)
        node_radius = max(trace.marker.size) / 2 + trace.marker.line.width / 2
        edge_radius = next(trace.line.width for trace in figure.data if trace.mode == "lines") / 2
        self.assertGreaterEqual(0.8 * min(xscale, yscale) - node_radius - edge_radius, 7.5)
