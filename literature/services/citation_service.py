#!/usr/bin/env python3

'''
@File:   citation_service.py

@Brief:  Within-library citation graph (no extra API calls).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import re
from collections import deque

# Custom library Imports
from literature.models import Discovery, OpenCitationsLookup, Paper
from literature.services.reference_service import normalize_doi

# ------------------------------------------------------------------------ #
# Graph building
# ------------------------------------------------------------------------ #

# --- Short OpenAlex id ---
def _short_id(raw: str) -> str:
    """Strip OpenAlex URL prefix to short W-id."""
    return str(raw or "").replace("https://openalex.org/", "").strip()


# --- Connectors between selected papers ---
def _connecting_papers(neighbours: dict[int, set[int]], seeds: set[int]) -> set[int]:
    """Find papers on a simple citation path between two selected papers.

    Build the block-cut forest: cyclic branches attached at only one point
    are not connections, while every route through a joining block is.
    """
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    blocks: list[set[int]] = []
    edge_stack: list[tuple[int, int]] = []

    for node in sorted(neighbours):
        if node in index:
            continue
        index[node] = low[node] = len(index)
        # Explicit DFS stack avoids Python's recursion limit on long chains.
        frames = [(node, None, iter(sorted(neighbours[node])))]
        while frames:
            current, parent, children = frames[-1]
            other = next(children, None)
            if other is None:
                frames.pop()
                if parent is not None:
                    low[parent] = min(low[parent], low[current])
                    if low[current] >= index[parent]:
                        block: set[int] = set()
                        while edge_stack:
                            src, tgt = edge_stack.pop()
                            block.update((src, tgt))
                            if (src, tgt) == (parent, current):
                                break
                        blocks.append(block)
                continue
            if other == parent:
                continue
            if other not in index:
                edge_stack.append((current, other))
                index[other] = low[other] = len(index)
                frames.append((other, current, iter(sorted(neighbours[other]))))
            elif index[other] < index[current]:
                edge_stack.append((current, other))
                low[current] = min(low[current], index[other])

    memberships: dict[int, list[int]] = {node: [] for node in neighbours}
    for block_id, block in enumerate(blocks):
        for node in block:
            memberships[node].append(block_id)

    # A block is a tree node; an articulation paper belongs to several
    # blocks and gets its own tree node. Mark each selected paper once.
    tree: dict[int, set[int]] = {i: set() for i in range(len(blocks))}
    anchor_counts: dict[int, int] = {}
    for node, member_blocks in memberships.items():
        if len(member_blocks) > 1:
            articulation = len(blocks) + node
            tree[articulation] = set(member_blocks)
            for block_id in member_blocks:
                tree[block_id].add(articulation)
            if node in seeds:
                anchor_counts[articulation] = 1
        elif node in seeds and member_blocks:
            block_id = member_blocks[0]
            anchor_counts[block_id] = anchor_counts.get(block_id, 0) + 1

    # Remove terminal blocks/points with no selected paper. What remains
    # is precisely the union of the routes joining selected anchors.
    remaining = set(tree)
    degree = {node: len(others) for node, others in tree.items()}
    queue = deque(node for node in tree if degree[node] <= 1 and node not in anchor_counts)
    while queue:
        node = queue.popleft()
        if node not in remaining:
            continue
        remaining.remove(node)
        for other in tree[node] & remaining:
            degree[other] -= 1
            if degree[other] <= 1 and other not in anchor_counts:
                queue.append(other)

    # A component with just one selected paper is a branch, not a story.
    result: set[int] = set()
    seen: set[int] = set()
    for start in sorted(remaining):
        if start in seen:
            continue
        stack = [start]
        component: set[int] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.add(node)
            stack.extend(tree[node] & remaining - seen)
        if sum(anchor_counts.get(node, 0) for node in component) >= 2:
            for block_id in component:
                if block_id < len(blocks):
                    result.update(blocks[block_id])
    return result - seeds


# --- Compact paths between selected papers ---
def _shortest_backbone(
    neighbours: dict[int, set[int]], seeds: set[int],
) -> set[frozenset[int]]:
    """Choose a deterministic small union of shortest paths spanning each group.

    Distances between selected papers form a metric graph. A minimum
    spanning forest on that graph chooses which pairs to join; one
    shortest citation path per chosen pair supplies the displayed edges.
    This is a compact backbone, not an exact minimum Steiner tree.
    """
    selected = sorted(seeds & neighbours.keys())
    ordered_neighbours = {pk: sorted(others) for pk, others in neighbours.items()}
    parents: dict[int, dict[int, int | None]] = {}
    candidates: list[tuple[int, int, int]] = []
    for i, start in enumerate(selected):
        predecessor: dict[int, int | None] = {start: None}
        distance = {start: 0}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for other in ordered_neighbours[node]:
                if other not in predecessor:
                    predecessor[other] = node
                    distance[other] = distance[node] + 1
                    queue.append(other)
        parents[start] = predecessor
        candidates.extend(
            (distance[end], start, end)
            for end in selected[i + 1:]
            if end in distance
        )

    leader = {pk: pk for pk in selected}

    def root(pk: int) -> int:
        while leader[pk] != pk:
            leader[pk] = leader[leader[pk]]
            pk = leader[pk]
        return pk

    backbone: set[frozenset[int]] = set()
    for _, start, end in sorted(candidates):
        first, second = root(start), root(end)
        if first == second:
            continue
        leader[second] = first
        node = end
        while node != start:
            previous = parents[start][node]
            if previous is None:
                break
            backbone.add(frozenset((previous, node)))
            node = previous
    return backbone


# --- Build graph ---
def build_graph(
    project, *, depth: int = 2, prune: bool = False,
    seed_stage: str = "final", min_chain: int = 1,
) -> dict:
    """Build nodes/edges from kept papers at one filtration point plus links.

    seed_stage picks the lens: "final" (Abstract Yes with references completed),
    "title" (Title Yes), or "found" (every paper found, no filtering).
    Anything else behaves like "found". Seeds draw in one colour, papers
    pulled in purely through citation links in another. Depth follows
    citing-to-cited links backward from selected papers, newest first.
    Prune keeps a shortest-path backbone joining selected papers in each
    component. min_chain hides whole groups smaller than the given size.
    Isolated non-seeds are always left out; isolated seeds are kept
    unless min_chain removes their group.
    """
    from literature.models import ScreeningDecision

    depth = max(0, min(int(depth or 0), 10))
    min_chain = max(1, int(min_chain or 1))
    if seed_stage not in {"final", "title", "found"}:
        seed_stage = "found"

    paper_ids = set(
        Discovery.objects.filter(search__project=project).values_list(
            "paper_id", flat=True
        )
    )
    if not paper_ids:
        return {"nodes": [], "edges": [], "skipped_isolated": 0,
                "missing_references": 0}

    papers = list(Paper.objects.filter(pk__in=paper_ids))
    by_short: dict[str, Paper] = {}
    by_doi: dict[str, Paper] = {}
    by_s2: dict[str, Paper] = {}
    for paper in papers:
        short = (paper.source_ids or {}).get("openalex") or paper.openalex_id
        if short:
            by_short[_short_id(short)] = paper
        if paper.doi:
            by_doi[normalize_doi(paper.doi)] = paper
        s2_id = (paper.source_ids or {}).get("semantic_scholar")
        if s2_id:
            by_s2[str(s2_id)] = paper

    # arXiv aliases are grounded in cached provider IDs, not guessed from titles.
    for paper in papers:
        arxiv_id = str((paper.source_ids or {}).get("arxiv") or "")
        match = re.fullmatch(r"(\d{4}\.\d{4,5})(?:v\d+)?", arxiv_id)
        if match:
            by_doi.setdefault("10.48550/arxiv." + match.group(1), paper)

    # All within-library edges: A lists B in references.
    adjacency: dict[int, set[int]] = {p.pk: set() for p in papers}
    for paper in papers:
        for ref in paper.references or []:
            raw = str(ref)
            target = (
                by_short.get(_short_id(raw))
                or by_doi.get(normalize_doi(raw))
                or (by_s2.get(raw[3:]) if raw.startswith("S2:") else None)
            )
            if target is not None and target.pk != paper.pk:
                adjacency[paper.pk].add(target.pk)

    # Explicitly fetched OpenCitations DOI edges can supply both missing
    # references and incoming citations. Never contact APIs while rendering.
    for lookup in OpenCitationsLookup.objects.filter(doi__in=by_doi):
        for pair in lookup.edges:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            citing = by_doi.get(normalize_doi(pair[0]))
            cited = by_doi.get(normalize_doi(pair[1]))
            if citing is not None and cited is not None and citing.pk != cited.pk:
                adjacency[citing.pk].add(cited.pk)

    # Seeds: kept papers at the chosen filtration point, then fallbacks.
    title_ids = set(
        ScreeningDecision.objects.filter(
            stage__project=project, stage__name="Title", decision="ACCEPT"
        ).values_list("paper_id", flat=True)
    ) & paper_ids
    from literature.services.screening_service import final_decisions

    abstract_ids = set(final_decisions(project).values_list("paper_id", flat=True)) & paper_ids
    if seed_stage == "title":
        seeds = set(title_ids)
    elif seed_stage == "final":
        seeds = set(abstract_ids)
    else:
        # No filtering: every found paper seeds, so linked papers always
        # stay however cited they are.
        seeds = set(paper_ids)

    # Connections are undirected for cluster detection, but exploration
    # follows only actual references from a citing paper to a cited paper.
    neighbours: dict[int, set[int]] = {p.pk: set() for p in papers}
    for src, targets in adjacency.items():
        for tgt in targets:
            neighbours[src].add(tgt)
            neighbours[tgt].add(src)
    by_pk = {p.pk: p for p in papers}

    def newest_first(pk: int) -> tuple[bool, int, int]:
        """Dated papers newest first; undated papers last, then stable PK."""
        year = by_pk[pk].publication_year
        return (year is None, -(year or 0), pk)

    # Every selected paper remains a starting point; the newest ones are
    # explored first and a disconnected older selection is never lost.
    visible = set(seeds)
    frontier = sorted(seeds, key=newest_first)
    for _ in range(depth):
        nxt: set[int] = set()
        for node in frontier:
            for other in sorted(adjacency[node], key=newest_first):
                if other not in visible:
                    visible.add(other)
                    nxt.add(other)
        frontier = sorted(nxt, key=newest_first)
        if not frontier:
            break

    # Drop isolated non-seed papers; seeds are always kept so final
    # papers never vanish from their own story, even with no links yet.
    linked = {n for n in visible if neighbours.get(n) & visible}
    skipped = len(visible - linked - set(seeds))
    visible = linked | set(seeds)

    subgraph = {n: neighbours[n] & visible for n in visible}
    visible_edges = sum(len(adjacency[n] & visible) for n in visible)

    if prune:
        backbone = _shortest_backbone(subgraph, seeds & visible)
        connectors = set().union(*backbone) - seeds if backbone else set()
        kept = (seeds | connectors) & visible
        pruned = len(visible - kept)
        visible = kept
    else:
        backbone = set()
        connectors = _connecting_papers(subgraph, seeds & visible)
        pruned = 0
    if min_chain > 1:
        # Hide outlier groups: drop whole linked groups smaller than
        # min_chain so only the main research lines remain.
        sub_all = {n: (neighbours.get(n, set()) & visible) for n in visible}
        seen_all: set[int] = set()
        for node in list(visible):
            if node in seen_all:
                continue
            stack = [node]
            group: set[int] = set()
            while stack:
                cur = stack.pop()
                if cur in seen_all:
                    continue
                seen_all.add(cur)
                group.add(cur)
                stack.extend(sub_all.get(cur, ()) - seen_all)
            if len(group) < min_chain:
                visible -= group
    # Grey always means "would survive pruning".
    joiners = connectors & visible
    nodes = []
    for pk in visible:
        paper = by_pk[pk]
        nodes.append(
            {
                "id": pk,
                "title": paper.title,
                "year": paper.publication_year,
                "citations": paper.citation_count or 0,
                "venue": paper.venue or "",
                "is_seed": pk in seeds,
                "is_joiner": pk in joiners,
            }
        )
    edges = [
        (src, tgt)
        for src, targets in adjacency.items()
        if src in visible
        for tgt in targets
        if tgt in visible and (not prune or frozenset((src, tgt)) in backbone)
    ]
    nodes.sort(key=lambda n: (n["year"] or 0, n["id"]))
    edges.sort()
    return {"nodes": nodes, "edges": edges, "skipped_isolated": skipped,
            "pruned": pruned, "pruned_edges": visible_edges - len(edges) if prune else 0,
            "missing_references": sum(not p.references for p in papers)}


# ------------------------------------------------------------------------ #
# Track summaries
# ------------------------------------------------------------------------ #

# --- Per-track table ---
def track_summaries(project, graph: dict) -> list[dict]:
    """One plain-English row per connected track.

    Keywords: most common fieldsOfStudy/tags on member papers.
    Search rows: which of your keyword rows hit members most.
    Years/venues: range and top venues (proxy for field/industry).
    """
    from collections import Counter

    from literature.models import Paper

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return []

    neighbour: dict[int, set[int]] = {n["id"]: set() for n in nodes}
    for src, tgt in edges:
        if src in neighbour and tgt in neighbour:
            neighbour[src].add(tgt)
            neighbour[tgt].add(src)
    seen: set[int] = set()
    components: list[set[int]] = []
    for node in nodes:
        if node["id"] in seen:
            continue
        stack = [node["id"]]
        current: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            current.add(cur)
            stack.extend(neighbour.get(cur, ()) - seen)
        components.append(current)

    papers = {p.pk: p for p in Paper.objects.filter(pk__in=[n["id"] for n in nodes])}
    search_rows: list[str] = []
    for search in project.searches.all().only("keywords"):
        for raw in search.keywords or []:
            text = str(raw).strip()
            if text and text not in search_rows:
                search_rows.append(text)

    # Linked stories only: lone papers with no links are dots on the
    # chart, not tracks in the table.
    components = [c for c in components if len(c) > 1]

    tracks: list[dict] = []
    for i, members in enumerate(
        sorted(components, key=len, reverse=True), start=1
    ):
        member_papers = [papers[pk] for pk in members if pk in papers]
        kw_counter: Counter = Counter()
        venue_counter: Counter = Counter()
        years: list[int] = []
        cites = 0
        for paper in member_papers:
            cites += paper.citation_count or 0
            if paper.publication_year:
                years.append(paper.publication_year)
            for kw in paper.keywords or []:
                if str(kw).strip():
                    kw_counter[str(kw).strip()] += 1
            if (paper.venue or "").strip():
                venue_counter[paper.venue.strip()] += 1
        row_hits: Counter = Counter()
        for row in search_rows:
            needle = row.lower()
            for paper in member_papers:
                hay = " ".join(
                    [str(paper.title or ""), str(paper.abstract or "")]
                    + [str(k) for k in paper.keywords or []]
                ).lower()
                if needle in hay:
                    row_hits[row] += 1
        tracks.append(
            {
                "name": f"Track {i}",
                "size": len(members),
                "citations": cites,
                "year_min": min(years) if years else None,
                "year_max": max(years) if years else None,
                "top_keywords": [k for k, _ in kw_counter.most_common(5)],
                "top_venues": [v for v, _ in venue_counter.most_common(3)],
                "top_rows": [r for r, _ in row_hits.most_common(3)],
            }
        )
    return tracks
