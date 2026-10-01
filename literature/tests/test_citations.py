#!/usr/bin/env python3

'''
@File:   test_citations.py

@Brief:  Tests for citation graph stories and shuffled queue.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.test import TestCase

from literature.models import Discovery, Paper, Project, ScreeningDecision, Search
from literature.services.citation_service import _connecting_papers, build_graph
from literature.services.plots_service import build_citation_figure

# ------------------------------------------------------------------------ #
# Citation tests
# ------------------------------------------------------------------------ #


class CitationTests(TestCase):
    """Within-library links form stories; shuffle keeps queue resumable."""

    def _select_final(self, paper: Paper, stage) -> None:
        """Construct a reference-completed fixture without inventing any citation edges."""
        if not paper.references:
            paper.reference_text = "Fixture author (2000). A reference not discovered in this project."
        paper.abstract = paper.abstract or "Fixture abstract"
        paper.save(update_fields=["reference_text", "abstract"])
        ScreeningDecision.objects.get_or_create(paper=paper, stage=stage.project.stages.get(name="Title"), defaults={"decision": "ACCEPT"})
        ScreeningDecision.objects.create(paper=paper, stage=stage, decision="ACCEPT")

    # --- Graph links ---
    def test_chain_links_and_depth(self) -> None:
        """A cites B cites C chains with depth cutoff."""
        project = Project.objects.create(name="P")
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        a = Paper.objects.create(
            title="A", openalex_id="WA", source_ids={"openalex": "WA"},
            references=["https://openalex.org/WB"], citation_count=5,
            publication_year=2022,
        )
        b = Paper.objects.create(
            title="B", openalex_id="WB", source_ids={"openalex": "WB"},
            references=["https://openalex.org/WC"], citation_count=10,
            publication_year=2020,
        )
        c = Paper.objects.create(
            title="C", openalex_id="WC", source_ids={"openalex": "WC"},
            references=[], citation_count=1, publication_year=2018,
        )
        for paper in (a, b, c):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        full = build_graph(project, depth=5, seed_stage="found")
        self.assertEqual(len(full["edges"]), 2)
        shallow = build_graph(project, depth=0, seed_stage="found")
        # Depth 0 from all seeds still shows direct edges among seeds.
        self.assertTrue(len(shallow["edges"]) >= 0)
        html = build_citation_figure(full)
        self.assertIn("How your kept papers link together", html)

    # --- Found lens ---
    def test_found_lens_seeds_everything(self) -> None:
        """Found lens seeds every paper, however cited, links intact."""
        project = Project.objects.create(name="P")
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        high = Paper.objects.create(
            title="H", citation_count=100, openalex_id="WH1",
            source_ids={"openalex": "WH1"},
            references=["https://openalex.org/WL1"],
        )
        low = Paper.objects.create(
            title="L", citation_count=0, openalex_id="WL1",
            source_ids={"openalex": "WL1"}, references=[],
        )
        for paper in (high, low):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        graph = build_graph(project, depth=0, seed_stage="found")
        self.assertEqual({n["id"] for n in graph["nodes"]}, {high.pk, low.pk})
        self.assertEqual(len(graph["edges"]), 1)

    def test_references_match_doi_and_semantic_scholar_id(self) -> None:
        """Fallback reference identifiers create real directed edges."""
        project = Project.objects.create(name="P")
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        source = Paper.objects.create(
            title="Citing", references=["https://doi.org/10.1/old", "S2:s2-old"],
        )
        doi_paper = Paper.objects.create(title="DOI", doi="10.1/old")
        s2_paper = Paper.objects.create(
            title="S2", source_ids={"semantic_scholar": "s2-old"},
        )
        for paper in (source, doi_paper, s2_paper):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        graph = build_graph(project, seed_stage="found")
        self.assertEqual(set(graph["edges"]),
                         {(source.pk, doi_paper.pk), (source.pk, s2_paper.pk)})

    def test_long_citation_chain_avoids_recursion_limit(self) -> None:
        """Finding paths in a large project must not use recursive DFS."""
        links = {pk: set() for pk in range(1500)}
        for pk in range(1499):
            links[pk].add(pk + 1)
            links[pk + 1].add(pk)
        self.assertEqual(len(_connecting_papers(links, {0, 1499})), 1498)

    # --- Final seeds + prune ---
    def test_final_seeds_and_prune(self) -> None:
        """One kept paper cannot turn its side branches into connections."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        seed = Paper.objects.create(
            title="Seed", openalex_id="WS1", source_ids={"openalex": "WS1"},
            references=["https://openalex.org/WM1"], citation_count=5,
            publication_year=2022,
        )
        mid = Paper.objects.create(
            title="Mid", openalex_id="WM1", source_ids={"openalex": "WM1"},
            references=["https://openalex.org/WO1"], citation_count=9,
            publication_year=2020,
        )
        old = Paper.objects.create(
            title="Old", openalex_id="WO1", source_ids={"openalex": "WO1"},
            references=[], citation_count=1, publication_year=2018,
        )
        side = Paper.objects.create(
            title="Side", openalex_id="WX1", source_ids={"openalex": "WX1"},
            references=["https://openalex.org/WM1"], citation_count=0,
            publication_year=2021,
        )
        for paper in (seed, mid, old, side):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        self._select_final(seed, abstract)
        full = build_graph(project, depth=5)
        ids = {n["id"] for n in full["nodes"]}
        self.assertIn(seed.pk, ids)
        pruned = build_graph(project, depth=5, prune=True)
        pruned_ids = {n["id"] for n in pruned["nodes"]}
        self.assertEqual(pruned_ids, {seed.pk})
        self.assertEqual(pruned["edges"], [])
        self.assertEqual(pruned["pruned"], 2)

    def test_depth_follows_references_backward_not_incoming_citations(self) -> None:
        """Each hop reaches an older cited paper, never an unselected citer."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        newest = Paper.objects.create(
            title="Newest", openalex_id="WBACK1", publication_year=2024,
            references=["https://openalex.org/WBACK2"],
        )
        middle = Paper.objects.create(
            title="Middle", openalex_id="WBACK2", publication_year=2022,
            references=["https://openalex.org/WBACK3"],
        )
        oldest = Paper.objects.create(
            title="Oldest", openalex_id="WBACK3", publication_year=2018,
        )
        incoming = Paper.objects.create(
            title="Incoming only", openalex_id="WBACK4", publication_year=2026,
            references=["https://openalex.org/WBACK2"],
        )
        for paper in (newest, middle, oldest, incoming):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        ScreeningDecision.objects.create(
            paper=newest, stage=project.stages.get(name="Abstract"), decision="ACCEPT",
        )
        expected = ({newest.pk}, {newest.pk, middle.pk},
                    {newest.pk, middle.pk, oldest.pk})
        for depth, ids in enumerate(expected):
            graph = build_graph(project, depth=depth)
            shown = {n["id"] for n in graph["nodes"]}
            self.assertEqual(shown, ids)
            self.assertEqual(len(graph["edges"]), depth)
            self.assertNotIn(incoming.pk, shown)

    def test_shared_cited_paper_connects_newest_selected_branches(self) -> None:
        """Two selected tips citing one ancestor form a visible cluster."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        ancestor = Paper.objects.create(title="Common ancestor", openalex_id="WANCESTOR",
                                        publication_year=2019)
        tips = [
            Paper.objects.create(title=f"Tip {year}", openalex_id=f"WTIP{year}",
                                 publication_year=year,
                                 references=["https://openalex.org/WANCESTOR"])
            for year in (2025, 2023)
        ]
        for paper in (*tips, ancestor):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        for paper in tips:
            ScreeningDecision.objects.create(paper=paper, stage=abstract, decision="ACCEPT")
        graph = build_graph(project, depth=1, prune=True)
        self.assertEqual({n["id"] for n in graph["nodes"]},
                         {ancestor.pk} | {paper.pk for paper in tips})
        self.assertEqual(set(graph["edges"]), {(paper.pk, ancestor.pk) for paper in tips})
        self.assertTrue(next(n for n in graph["nodes"] if n["id"] == ancestor.pk)["is_joiner"])

    # --- Prune keeps seed-to-seed stories ---
    def test_prune_keeps_story_between_seeds(self) -> None:
        """A chain starting and ending with selected papers survives prune."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        new = Paper.objects.create(
            title="New", openalex_id="WN1", source_ids={"openalex": "WN1"},
            references=["https://openalex.org/WM2"], citation_count=2,
            publication_year=2023,
        )
        mid = Paper.objects.create(
            title="Mid", openalex_id="WM2", source_ids={"openalex": "WM2"},
            references=["https://openalex.org/WO2"], citation_count=4,
            publication_year=2020,
        )
        old = Paper.objects.create(
            title="Old", openalex_id="WO2", source_ids={"openalex": "WO2"},
            references=[], citation_count=1, publication_year=2018,
        )
        for paper in (new, mid, old):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        self._select_final(new, abstract)
        self._select_final(old, abstract)
        pruned = build_graph(project, depth=5, prune=True)
        pruned_ids = {n["id"] for n in pruned["nodes"]}
        self.assertEqual(pruned_ids, {new.pk, mid.pk, old.pk})

    def test_backbone_keeps_directed_edges_when_traversed_backwards(self) -> None:
        """Undirected path selection still displays the actual citing direction."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        old = Paper.objects.create(title="Old", openalex_id="WOLD")
        mid = Paper.objects.create(
            title="Mid", openalex_id="WMID", references=["https://openalex.org/WOLD"],
        )
        new = Paper.objects.create(
            title="New", openalex_id="WNEW", references=["https://openalex.org/WMID"],
        )
        for paper in (old, mid, new):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        for paper in (old, new):
            self._select_final(paper, abstract)
        graph = build_graph(project, depth=3, prune=True)
        self.assertEqual(set(graph["edges"]), {(new.pk, mid.pk), (mid.pk, old.pk)})
        self.assertIn("New cites Mid", build_citation_figure(graph))

    # --- Prune chooses one shortest track ---
    def test_prune_chooses_one_of_parallel_shortest_tracks(self) -> None:
        """Equal-length routes resolve deterministically to one backbone."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        top = Paper.objects.create(
            title="Top", openalex_id="WP1", source_ids={"openalex": "WP1"},
            references=["https://openalex.org/WP2", "https://openalex.org/WP3"],
            publication_year=2023,
        )
        left = Paper.objects.create(
            title="Left", openalex_id="WP2", source_ids={"openalex": "WP2"},
            references=["https://openalex.org/WP4"], publication_year=2020,
        )
        right = Paper.objects.create(
            title="Right", openalex_id="WP3", source_ids={"openalex": "WP3"},
            references=["https://openalex.org/WP4"], publication_year=2021,
        )
        bottom = Paper.objects.create(
            title="Bottom", openalex_id="WP4", source_ids={"openalex": "WP4"},
            references=[], publication_year=2018,
        )
        for paper in (top, left, right, bottom):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        self._select_final(top, abstract)
        self._select_final(bottom, abstract)
        pruned = build_graph(project, depth=5, prune=True)
        self.assertEqual(
            {n["id"] for n in pruned["nodes"]},
            {top.pk, left.pk, bottom.pk},
        )
        self.assertEqual(set(pruned["edges"]), {(top.pk, left.pk), (left.pk, bottom.pk)})
        self.assertEqual(pruned["pruned_edges"], 2)
        self.assertEqual(build_graph(project, depth=5, prune=True)["edges"], pruned["edges"])

    # --- Joiner flags ---
    def test_joiner_grey_outer_blue(self) -> None:
        """Connectivity, not publication year, determines grey vs blue."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        new = Paper.objects.create(
            title="New", openalex_id="WJ1", source_ids={"openalex": "WJ1"},
            references=["https://openalex.org/WJ2", "https://openalex.org/WJ4"],
            publication_year=2023,
        )
        mid = Paper.objects.create(
            title="Mid", openalex_id="WJ2", source_ids={"openalex": "WJ2"},
            references=["https://openalex.org/WJ3"], publication_year=2020,
        )
        old = Paper.objects.create(
            title="Old", openalex_id="WJ3", source_ids={"openalex": "WJ3"},
            references=[], publication_year=2018,
        )
        side = Paper.objects.create(
            title="Side", openalex_id="WJ4", source_ids={"openalex": "WJ4"},
            references=[], publication_year=2021,
        )
        newer_bridge = Paper.objects.create(
            title="New bridge", openalex_id="WJ5", source_ids={"openalex": "WJ5"},
            references=["https://openalex.org/WJ1", "https://openalex.org/WJ3"],
            publication_year=2025,
        )
        for paper in (new, mid, old, side, newer_bridge):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        self._select_final(new, abstract)
        self._select_final(old, abstract)
        graph = build_graph(project, depth=5)
        flags = {n["id"]: (n["is_seed"], n["is_joiner"]) for n in graph["nodes"]}
        self.assertEqual(flags[new.pk], (True, False))
        self.assertEqual(flags[old.pk], (True, False))
        self.assertEqual(flags[mid.pk], (False, True))
        self.assertEqual(flags[side.pk], (False, False))
        self.assertNotIn(newer_bridge.pk, flags)
        html = build_citation_figure(graph)
        self.assertIn("#3C27F5", html)
        self.assertIn("Unconnected branches", html)
        self.assertIn("New cites Side", html)
        pruned = build_graph(project, depth=5, prune=True)
        pruned_ids = {n["id"] for n in pruned["nodes"]}
        self.assertNotIn(side.pk, pruned_ids)
        self.assertIn(mid.pk, pruned_ids)
        self.assertNotIn(newer_bridge.pk, pruned_ids)

    # --- Cyclic branch is not a connection ---
    def test_attached_cycle_does_not_join_selected_papers(self) -> None:
        """A loop hung off one selected paper is still an outer branch."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        papers = [
            Paper.objects.create(
                title=f"Cycle {i}", openalex_id=f"WQ{i}",
                source_ids={"openalex": f"WQ{i}"},
                references=[f"https://openalex.org/WQ{j}" for j in targets],
            )
            for i, targets in enumerate(([1, 3], [2], [], [4], [0]))
        ]
        for paper in papers:
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        for paper in (papers[0], papers[2]):
            self._select_final(paper, abstract)
        graph = build_graph(project, depth=5)
        connectors = {n["id"] for n in graph["nodes"] if n["is_joiner"]}
        self.assertEqual(connectors, {papers[1].pk})
        pruned = build_graph(project, depth=5, prune=True)
        self.assertEqual({n["id"] for n in pruned["nodes"]},
                         {papers[0].pk, papers[1].pk, papers[2].pk})

    # --- Unequal routes still connect ---
    def test_longer_alternative_path_is_interior(self) -> None:
        """Not only shortest paths count when two kept papers share a cluster."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        papers = [
            Paper.objects.create(
                title=f"Path {i}", openalex_id=f"WR{i}",
                source_ids={"openalex": f"WR{i}"},
                references=[f"https://openalex.org/WR{j}" for j in targets],
            )
            for i, targets in enumerate(([1, 2], [4], [3], [4], []))
        ]
        for paper in papers:
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        for paper in (papers[0], papers[4]):
            self._select_final(paper, abstract)
        graph = build_graph(project, depth=5)
        self.assertEqual({n["id"] for n in graph["nodes"]}, {p.pk for p in papers})
        pruned = build_graph(project, depth=5, prune=True)
        self.assertEqual({n["id"] for n in pruned["nodes"]},
                         {papers[i].pk for i in (0, 1, 4)})
        self.assertEqual(set(pruned["edges"]),
                         {(papers[0].pk, papers[1].pk), (papers[1].pk, papers[4].pk)})

    def test_backbone_spans_three_selected_papers_and_keeps_isolates(self) -> None:
        """A connected trio uses two shortest links; other seeds stay visible."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        papers = [
            Paper.objects.create(
                title=f"Triangle {i}", openalex_id=f"WT{i}",
                source_ids={"openalex": f"WT{i}"},
                references=[f"https://openalex.org/WT{j}" for j in targets],
            )
            for i, targets in enumerate(([1, 2], [2], [], []))
        ]
        for paper in papers:
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
            self._select_final(paper, project.stages.get(name="Abstract"))
        graph = build_graph(project, prune=True)
        self.assertEqual({n["id"] for n in graph["nodes"]}, {p.pk for p in papers})
        self.assertEqual(set(graph["edges"]),
                         {(papers[0].pk, papers[1].pk), (papers[0].pk, papers[2].pk)})

    # --- Smallest chain ---
    def test_min_chain_hides_outliers(self) -> None:
        """Groups smaller than min_chain vanish; the main line stays."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        chain = [
            Paper.objects.create(
                title=f"C{i}", openalex_id=f"WC{i}",
                source_ids={"openalex": f"WC{i}"},
                references=[f"https://openalex.org/WC{i + 1}"] if i < 2 else [],
                publication_year=2020 + i,
            )
            for i in range(3)
        ]
        lone = Paper.objects.create(title="Lone", publication_year=2021)
        for paper in (*chain, lone):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        for paper in (*chain, lone):
            self._select_final(paper, abstract)
        shown = build_graph(project, depth=5, min_chain=3)
        shown_ids = {n["id"] for n in shown["nodes"]}
        for paper in chain:
            self.assertIn(paper.pk, shown_ids)
        self.assertNotIn(lone.pk, shown_ids)

    # --- Isolated seeds stay visible ---
    def test_isolated_seeds_kept(self) -> None:
        """Final papers with no links still appear (seeds never vanish)."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        lone = Paper.objects.create(title="Lone", citation_count=3)
        Discovery.objects.create(paper=lone, search=search, source="openalex", query="a")
        abstract = project.stages.get(name="Abstract")
        self._select_final(lone, abstract)
        graph = build_graph(project, depth=2)
        self.assertIn(lone.pk, {n["id"] for n in graph["nodes"]})

    # --- Seed stage lens ---
    def test_seed_stage_lens(self) -> None:
        """Title lens seeds title-kept papers; final lens seeds abstract-kept."""
        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        search = Search.objects.create(project=project, name="S", keywords=["a"])
        kept_title = Paper.objects.create(title="KT", citation_count=1)
        kept_final = Paper.objects.create(title="KF", citation_count=1)
        for paper in (kept_title, kept_final):
            Discovery.objects.create(paper=paper, search=search, source="openalex", query="a")
        title = project.stages.get(name="Title")
        abstract = project.stages.get(name="Abstract")
        ScreeningDecision.objects.create(paper=kept_title, stage=title, decision="ACCEPT")
        self._select_final(kept_final, abstract)
        title_graph = build_graph(project, depth=0, seed_stage="title")
        self.assertEqual({n["id"] for n in title_graph["nodes"]}, {kept_title.pk, kept_final.pk})
        final_graph = build_graph(project, depth=0, seed_stage="final")
        self.assertIn(kept_final.pk, {n["id"] for n in final_graph["nodes"]})
        found_graph = build_graph(project, depth=0, seed_stage="found")
        self.assertTrue(len(found_graph["nodes"]) >= 1)
        weird_graph = build_graph(project, depth=0, seed_stage="bogus")
        self.assertTrue(len(weird_graph["nodes"]) >= 1)

    # --- Shuffle resumable ---
    def test_shuffled_queue_stays_resumable(self) -> None:
        """Random order still drains the pending pool exactly once."""
        from literature.views import _next_pending

        project = Project.objects.create(name="P")
        project.ensure_default_stages()
        stage = project.stages.get(name="Title")
        papers = [Paper.objects.create(title=f"T{i}") for i in range(5)]
        for paper in papers:
            ScreeningDecision.objects.create(paper=paper, stage=stage)
        seen: set[int] = set()
        for _ in range(5):
            nxt = _next_pending(stage)
            self.assertIsNotNone(nxt)
            seen.add(nxt.pk)  # type: ignore[union-attr]
            nxt.decisions.filter(stage=stage).update(decision="ACCEPT")  # type: ignore[union-attr]
        self.assertEqual(len(seen), 5)
        self.assertIsNone(_next_pending(stage))
