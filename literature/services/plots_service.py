#!/usr/bin/env python3

'''
@File:   plots_service.py

@Brief:  Plotly figures for the project analysis section.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

from collections import deque
from html import escape
from itertools import combinations, islice
from math import ceil, cos, exp, fsum, hypot, isfinite, log1p, pi, sin, sqrt
from pathlib import Path
from random import Random

# Custom library Imports
import plotly.graph_objects as go

# ------------------------------------------------------------------------ #
# Keyword hits figure
# ------------------------------------------------------------------------ #

# --- Keyword funnel bars ---


def _select_step(fig: go.Figure, step: str) -> None:
    """
    Keep the requested trace without changing its point membership.

    Args:
        fig: Figure with found, title and final-gated abstract traces.
        step: Validated all/found/title/final display selection.

    Kwargs:
        None

    Returns:
        None
    """
    if step != "all":
        selected = "abstract" if step == "final" else step
        fig.data = tuple(
            trace for key, trace in zip(
                ("found", "title", "abstract"), fig.data) if key == selected)


def build_keyword_figure(
    counts: dict[str, dict[str, int]], *, order: str = "az",
    step: str = "all", plot_config: dict | None = None,
) -> str:
    """
    Grouped bars per keyword: Found vs Title Yes vs Abstract Yes.

    Plain English labels; muted colours; decreasing heights show filtering.
    Returns an embeddable HTML div (Plotly.js loaded locally from the app).

    Args:
        counts: Ordered keyword counts for found/title/final-gated abstract.

    Kwargs:
        order: az preserves input order; found_desc sorts counts. Default az.
        step: Display all/found/title/final. Defaults to all.
        plot_config: Plotly configuration overrides. Defaults to None.

    Returns:
        str: Embeddable figure HTML without the Plotly bundle.
    """
    keywords = list(counts.keys())
    if order == "found_desc":
        keywords.sort(key=lambda word: (
            -counts[word]["found"], word.casefold(), word))
    found = [counts[k]["found"] for k in keywords]
    title = [counts[k]["title"] for k in keywords]
    abstract = [counts[k]["abstract"] for k in keywords]

    fig = go.Figure()
    fig.add_bar(
        name="Found papers",
        x=keywords,
        y=found,
        marker_color="#8aa8c8",
        opacity=0.55,
        hovertemplate="Keyword: %{x}<br>Found papers: %{y}<extra></extra>",
    )
    fig.add_bar(
        name="Kept after title check",
        x=keywords,
        y=title,
        marker_color="#6f9e6f",
        opacity=0.75,
        hovertemplate="Keyword: %{x}<br>Kept after title check: %{y}<extra></extra>",
    )
    fig.add_bar(
        name="Kept after abstract check",
        x=keywords,
        y=abstract,
        marker_color="#c86f2e",
        opacity=0.95,
        hovertemplate="Keyword: %{x}<br>Kept after abstract check: %{y}<extra></extra>",
    )
    fig.update_layout(
        barmode="overlay",
        title="How many papers each keyword found, and how many survived filtering",
        xaxis_title="Keyword (one row from your search)",
        yaxis_title="Number of papers",
        xaxis_tickangle=-45,
        legend_title="Step",
        margin=dict(b=200),
    )
    _select_step(fig, step)
    return fig.to_html(
        full_html=False, include_plotlyjs=False, config=plot_config)


# ------------------------------------------------------------------------ #
# Source pie / funnel / time plots
# ------------------------------------------------------------------------ #

# --- Where raw records and removals came from ---
def source_pie_figure(
    per_source: dict[str, dict[str, int]], *, metric: str = "results",
) -> go.Figure:
    """
    Show each source's share of one audited category.

    Duplicate and incomplete slices are shares of all such outcomes. Their
    hover also shows the rate within that source's own raw record count.
    Incomplete papers are gated out of screening, not deleted from SQLite.
    """
    titles = {
        "results": "Where the found records came from",
        "dups": "Where duplicate hits were removed",
        "incomplete": "Where incomplete papers were excluded from screening",
    }
    if metric not in titles:
        raise ValueError(f"Unknown source pie metric: {metric}")
    names = {
        "openalex": "OpenAlex",
        "semantic_scholar": "Semantic Scholar",
        "crossref": "Crossref",
        "arxiv": "arXiv",
        "scopus": "Scopus",
        "google_scholar": "Google Scholar",
        "publisher": "Publisher / journal website",
        "reference_list": "Reference list",
        "other": "Other source",
        "manual": "Unspecified at import (historical)",
    }
    colors = {
        "openalex": "#4c78a8",
        "semantic_scholar": "#e5a33d",
        "crossref": "#8064a6",
        "arxiv": "#3f9d91",
        "scopus": "#bc6b32",
        "manual": "#64748b",
    }
    ordered = [key for key in names if key in per_source]
    ordered.extend(sorted(key for key in per_source
                          if key not in names and str(key).strip()))
    values = [max(0, int(per_source[key].get(metric, 0) or 0))
              for key in ordered]
    total = sum(values)
    fig = go.Figure()
    if total:
        rates = [
            100 * value /
            raw if (raw := per_source[key].get("results", 0)) else 0
            for key, value in zip(ordered, values)
        ]
        hover = (
            "Source: %{label}<br>Raw records: %{value}<br>Share of all raw records: %{percent:.1%}"
            if metric == "results" else
            "Source: %{label}<br>Count: %{value}<br>Share of this outcome: %{percent:.1%}"
            "<br>Of this source's raw records: %{customdata:.1f}%"
        )
        fig.add_trace(go.Pie(
            labels=[names.get(key, key) for key in ordered],
            values=values,
            marker=dict(colors=[colors.get(key, "#718096")
                        for key in ordered]),
            customdata=rates,
            textinfo="label+percent",
            hovertemplate=(
                hover + "<br>Historical import audit; not current missing-source status.<extra></extra>"
            ),
        ))
    else:
        fig.add_annotation(
            text="No records in this category yet", x=0.5, y=0.5,
            xref="paper", yref="paper", showarrow=False,
        )
    fig.update_layout(title=f"{titles[metric]} ({total} total)", height=440,
                      margin=dict(l=40, r=40, t=80, b=40))
    return fig


# --- Current-state source bars (distinct papers; multi-source explicit) ---
def current_source_figure(
    current: dict, *, order: str = "canonical",
) -> go.Figure:
    """
    Plot only named current sources without changing source provenance.

    A paper with several sources counts once per bar, so bar heights may sum
    past the distinct-paper total. Blank counts remain in the metrics and
    page explanation, not as a source category on this plot.

    Args:
        current: Distinct-paper, blank and per-source counts; blank is not
            plotted as a source.

    Kwargs:
        order: canonical/name/count_desc bar ordering. Defaults to canonical.

    Returns:
        go.Figure: Named source bars with unchanged overlapping counts.
    """
    names = {
        "openalex": "OpenAlex",
        "semantic_scholar": "Semantic Scholar",
        "crossref": "Crossref",
        "arxiv": "arXiv",
        "scopus": "Scopus",
        "google_scholar": "Google Scholar",
        "publisher": "Publisher / journal website",
        "reference_list": "Reference list",
        "other": "Other source",
    }
    per_source = (current or {}).get("per_source", {})
    unique = int((current or {}).get("unique", 0) or 0)
    ordered = [key for key in names if key in per_source]
    ordered.extend(sorted(set(per_source) - set(names)))
    labels = [names.get(key, key) for key in ordered]
    values = [max(0, int(per_source.get(key, 0) or 0))
              for key in ordered]
    if order != "canonical" and labels:
        bars = sorted(zip(labels, values), key=(
            (lambda pair: pair[0].casefold()) if order == "name"
            else (lambda pair: (-pair[1], pair[0].casefold()))))
        labels, values = map(list, zip(*bars))
    fig = go.Figure()
    if any(values):
        fig.add_bar(
            x=labels,
            y=values,
            marker_color="#4c78a8",
            hovertemplate="Source: %{x}<br>Distinct papers: %{y}<extra></extra>",
        )
    else:
        fig.add_annotation(
            text=("No named sources to plot" if unique else
                  "No papers in this project yet"), x=0.5, y=0.5,
            xref="paper", yref="paper", showarrow=False,
        )
    fig.update_layout(
        title=f"Current papers by source ({unique} distinct papers)",
        xaxis_title="Source (current display; one paper may appear in several bars)",
        yaxis_title="Distinct papers",
        xaxis_tickangle=-30,
        margin=dict(l=40, r=40, t=80, b=180),
    )
    return fig


def build_current_source_bars(
    current: dict, *, order: str = "canonical",
    plot_config: dict | None = None,
) -> str:
    """
    One embeddable current-state bar chart with its own download button.

    Args:
        current: Distinct-paper, blank and per-source counts.

    Kwargs:
        order: canonical/name/count_desc ordering. Defaults to canonical.
        plot_config: Plotly configuration overrides. Defaults to None.

    Returns:
        str: Embeddable source-bar HTML.
    """
    return current_source_figure(current, order=order).to_html(
        full_html=False, include_plotlyjs=False, config=plot_config)


def build_source_pie(
    per_source: dict[str, dict[str, int]], *, metric: str = "results",
) -> str:
    """
    One embeddable Plotly pie with its own camera download button.
    """
    fig = source_pie_figure(per_source, metric=metric)
    return fig.to_html(full_html=False, include_plotlyjs=False)


# --- Filtering flow (PRISMA-style Sankey, left to right) ---
def build_funnel_figure(
    metrics: dict, *, counts: bool = True, plot_config: dict | None = None,
) -> str:
    """
    Left-to-right flow showing how papers split at each check.

    Dropped lanes sit above, accepted lanes at the bottom. Node labels
    stay short (Plotly draws rightmost labels over the bands, so long
    text overlaps); counts live on the bands themselves via link labels
    plus hover text.

    Args:
        metrics: Project results and stage counts.

    Kwargs:
        counts: Show band count labels and hover counts. Defaults to True.
        plot_config: Plotly configuration overrides. Defaults to None.

    Returns:
        str: Embeddable flow HTML retaining every category and band.
    """
    stages = metrics.get("stages", {})
    found = metrics.get("total_identified", 0)
    unique = metrics.get("unique_total", 0)
    dups = metrics.get("total_dups", max(found - unique, 0))
    title_yes = stages.get("Title", {}).get("accept", 0)
    title_no = stages.get("Title", {}).get("reject", 0)
    abstract_yes = stages.get("Abstract", {}).get("accept", 0)
    abstract_no = stages.get("Abstract", {}).get("reject", 0)
    labels = [
        "Records found",
        "Duplicates",
        "Unique papers",
        "Title: dropped",
        "Title: kept",
        "Abstract: dropped",
        "Abstract: kept",
        "Title: pending",
        "Abstract: pending / references",
    ]
    sources = [0, 0, 2, 2, 2, 4, 4, 4]
    targets = [1, 2, 3, 4, 7, 5, 6, 8]
    values = [dups, unique, title_no, title_yes, stages.get("Title", {}).get(
        "pending", 0), abstract_no, abstract_yes, stages.get("Abstract", {}).get("pending", 0)]
    values = [max(int(v or 0), 0) for v in values]
    if sum(values) == 0:
        values = [0] * len(sources)
    fig = go.Figure(
        go.Sankey(
            arrangement="snap",
            node=dict(
                label=labels,
                pad=20,
                color=["#8aa8c8", "#bbbbbb", "#9dbf9d", "#c88a8a",
                       "#6f9e6f", "#a85a5a", "#3f7e3f", "#c4b78c", "#c4b78c"],
            ),
            link=dict(
                source=sources,
                target=targets,
                value=values,
                label=[str(v) if counts else "" for v in values],
                hovertemplate=(
                    "From %{source.label} to %{target.label}"
                    + (": %{value} papers" if counts else "")
                    + "<extra></extra>"),
            ),
        )
    )
    fig.update_layout(
        title="How papers flow through each check (left to right)",
        font=dict(size=12),
    )
    if metrics.get("title_not_queued", 0):
        fig.add_annotation(text=f"{metrics['title_not_queued']} stored papers are not queued for Title screening.<br>They are not pending decisions or rejections; missing titles need completion.",
                           xref="paper", yref="paper", x=0, y=0, yshift=-70, xanchor="left", yanchor="top", showarrow=False)
        fig.update_layout(margin={"b": 120})
    return fig.to_html(
        full_html=False, include_plotlyjs=False, config=plot_config)


# --- Papers per year ---
def build_yearly_figure(
    data: dict | list | tuple, *, step: str = "all",
    plot_config: dict | None = None,
) -> str:
    """
    Bars per year, layered by step (largest behind).

    Accepts the stage_year_cumulative dict (preferred) or a plain year
    list (single layer, backward compatible). The ring left showing
    around a colour = papers from that year lost at that check.

    Args:
        data: Dated stage counts, or a legacy list/tuple of years.

    Kwargs:
        step: Display all/found/title/final. Defaults to all.
        plot_config: Plotly configuration overrides. Defaults to None.

    Returns:
        str: Embeddable IN-year bars with matching popup series identifiers.
    """
    from collections import Counter

    if isinstance(data, dict) and data.get("years"):
        years = data["years"]
        yearly = data.get("yearly", {})
        fig = go.Figure()
        fig.add_bar(
            name="Found papers", x=years, y=yearly.get("found", []),
            marker_color="#8aa8c8", opacity=0.55,
            hovertemplate="Year: %{x}<br>Found papers: %{y}<extra></extra>",
        )
        fig.add_bar(
            name="Kept after title check", x=years, y=yearly.get("title", []),
            marker_color="#6f9e6f", opacity=0.75,
            hovertemplate="Year: %{x}<br>Kept after title check: %{y}<extra></extra>",
        )
        fig.add_bar(
            name="Kept after abstract check", x=years, y=yearly.get("abstract", []),
            marker_color="#c86f2e", opacity=0.95,
            hovertemplate="Year: %{x}<br>Kept after abstract check: %{y}<extra></extra>",
        )
        fig.update_layout(
            barmode="overlay",
            title="Papers per year at each step",
            xaxis_title="Year",
            yaxis_title="Number of papers",
            legend_title="Step",
        )
        for trace, key in zip(fig.data, ("found", "title", "abstract")):
            trace.customdata = [[key, "year", year] for year in years]
        _select_step(fig, step)
        return fig.to_html(
            full_html=False, include_plotlyjs=False, config=plot_config,
            post_script="window.dispatchEvent(new CustomEvent("
            "'survey-chart-ready', {detail: '{plot_id}'}));")
    years_list = list(data) if isinstance(data, (list, tuple)) else []
    counts = Counter(y for y in years_list if y)
    ordered = sorted(counts)
    fig = go.Figure(
        go.Bar(
            x=ordered,
            y=[counts[y] for y in ordered],
            marker_color="#8aa8c8",
            hovertemplate="Year: %{x}<br>Papers: %{y}<extra></extra>",
        )
    )
    fig.update_layout(
        title="Papers published each year",
        xaxis_title="Year",
        yaxis_title="Number of papers",
    )
    return fig.to_html(
        full_html=False, include_plotlyjs=False, config=plot_config)


# ------------------------------------------------------------------------ #
# Cumulative by stage over years
# ------------------------------------------------------------------------ #

# --- Stage running totals ---
def build_stage_cumulative_figure(
    data: dict, *, step: str = "all", inline_controls: bool = False,
    plot_config: dict | None = None,
) -> str:
    """
    Running totals per year, one line per filter step.

    Gaps between lines in an era = papers from those years filtered out.
    Dropdown isolates one step or compares all.
    A box plot would not fit here (boxes show spread, not growth over time).

    Args:
        data: THROUGH-year series calculated before any display window.

    Kwargs:
        step: Display all/found/title/final. Defaults to all.
        inline_controls: Replace the series menu with page controls.
            Defaults to False.
        plot_config: Plotly configuration overrides. Defaults to None.

    Returns:
        str: Embeddable cumulative HTML with matching popup series keys.
    """
    years = data.get("years", [])
    series = data.get("series", {})
    undated = data.get("undated", {})
    footnote = (
        f"Note: {undated.get('found', 0)} papers have no year.<br>"
        "They are not drawn in these lines.<br>"
        f"Title: {undated.get('title', 0)}; final selected: {undated.get('abstract', 0)}."
        if undated.get("found", 0)
        else ("All papers have a year.<br>None omitted." if years else "No dated papers to plot yet.")
    )
    footer = {
        "text": footnote, "xref": "paper", "yref": "paper", "x": 0.0,
        "y": 0, "yshift": -100, "xanchor": "left", "yanchor": "top",
        "showarrow": False, "font": {"size": 11, "color": "#555555"}, "align": "left",
    }
    if not years:
        fig = go.Figure()
        fig.update_layout(
            title="Running totals by year (no dated papers yet)",
            xaxis_title="Year (oldest on the left)",
            yaxis_title="Total papers so far",
            xaxis={"tickmode": "linear", "dtick": 1, "tickformat": "d"},
            margin={"b": 180},
        )
        fig.add_annotation(**footer)
        return fig.to_html(
            full_html=False, include_plotlyjs=False, config=plot_config)

    def _trace(name: str, values: list[int], color: str) -> go.Scatter:
        """
        Build one cumulative trace with the displayed year coordinates.

        Args:
            name: Human-readable stage label.
            values: Cumulative counts for the displayed years.
            color: Trace color understood by Plotly.

        Kwargs:
            None

        Returns:
            go.Scatter: Labeled line and marker trace.
        """
        return go.Scatter(
            x=years,
            y=values,
            mode="lines+markers",
            name=name,
            line=dict(color=color),
            hovertemplate="Year: %{x}<br>" + name +
            ": %{y} so far<extra></extra>",
        )

    fig = go.Figure()
    fig.add_trace(_trace("Found papers", series.get("found", []), "#8aa8c8"))
    fig.add_trace(_trace("Kept after title check",
                  series.get("title", []), "#6f9e6f"))
    fig.add_trace(_trace("Kept after abstract check",
                  series.get("abstract", []), "#c8a06f"))
    # Prefer readable 1/2/5/10-year intervals, never more than ten years.
    span = max(years) - min(years)
    interval = next((step for step in (1, 2, 5, 10) if span <= step * 8), 10)
    tick_start = (min(years) // interval) * interval
    fig.update_layout(
        title="Running totals by year at each filter step",
        xaxis={
            "title": "Year (oldest on the left)", "tickmode": "linear",
            "type": "linear", "tickformat": "d", "tick0": tick_start, "dtick": interval,
        },
        yaxis_title="Total papers so far",
        legend_title="Step",
        updatemenus=[
            dict(
                type="dropdown",
                direction="down",
                showactive=True,
                x=0.0,
                y=1.15,
                buttons=[
                    dict(label="Compare: all steps", method="update",
                         args=[{"visible": [True, True, True]}]),
                    dict(label="Found only", method="update",
                         args=[{"visible": [True, False, False]}]),
                    dict(label="Title-kept only", method="update",
                         args=[{"visible": [False, True, False]}]),
                    dict(label="Abstract-kept only", method="update",
                         args=[{"visible": [False, False, True]}]),
                ],
            ),
        ],
    )
    fig.add_annotation(**footer)
    fig.update_layout(margin={"b": 180})
    for trace, key in zip(fig.data, ("found", "title", "abstract")):
        trace.customdata = [[key, "through", year] for year in years]
    _select_step(fig, step)
    if inline_controls or step != "all":
        fig.layout.updatemenus = []
    return fig.to_html(
        full_html=False, include_plotlyjs=False, config=plot_config,
        post_script="window.dispatchEvent(new CustomEvent("
        "'survey-chart-ready', {detail: '{plot_id}'}));")


# ------------------------------------------------------------------------ #
# Citation network (2D)
# ------------------------------------------------------------------------ #

# --- Readability-first citation positions ---
def _layout_quality(
    points: dict[int, tuple[float, float]], edges: list
) -> tuple[tuple[int, int, float], set[int]]:
    """
    Score proper crossings, unrelated nodes near edges, then squared edge length.

    Coordinates are dimensionless display units. Shared endpoints are not
    crossings; 0.8 units keeps unrelated lines clear of the bounded markers.
    The small orientation tolerance avoids counting floating-point collinearity.
    """
    crossings = intrusions = 0
    trouble: set[int] = set()

    def side(a: int, b: int, c: int) -> float:
        ax, ay = points[a]
        bx, by = points[b]
        cx, cy = points[c]
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

    for (a, b), (c, d) in combinations(edges, 2):
        if len({a, b, c, d}) == 4 and (
            side(a, b, c) * side(a, b, d) < -
            1e-9 and side(c, d, a) * side(c, d, b) < -1e-9
        ):
            crossings += 1
            trouble.update((a, b, c, d))
    lengths = []
    for a, b in edges:
        ax, ay = points[a]
        dx, dy = points[b][0] - ax, points[b][1] - ay
        length_squared = dx * dx + dy * dy
        lengths.append(length_squared)
        for pk, (x, y) in points.items():
            if pk in (a, b):
                continue
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) /
                    max(length_squared, 1e-9)))
            if (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 < 0.8**2:
                intrusions += 1
                trouble.add(pk)
    return (crossings, intrusions, fsum(lengths)), trouble


def _spaced_positions(
    points: dict[int, tuple[float, float]], *, shrink: bool = False
) -> dict[int, tuple[float, float]]:
    """
    Guarantee at least 2.5 display units between centres by uniform scaling.
    """
    if any(not isfinite(value) for point in points.values() for value in point):
        raise ValueError("Non-finite graph coordinates.")
    distance = min(
        (hypot(a[0] - b[0], a[1] - b[1]) for a, b in combinations(points.values(), 2)), default=2.5
    )
    if distance < 1e-6:
        raise ValueError("Coincident graph coordinates.")
    scale = 2.5 / distance if shrink else max(1.0, 2.5 / distance)
    spaced = {pk: (x * scale, y * scale) for pk, (x, y) in points.items()}
    if any(not isfinite(value) for point in spaced.values() for value in point):
        raise ValueError("Graph coordinate scaling overflow.")
    return spaced


def _spring_positions(order: list[int], edges: list) -> dict[int, tuple[float, float]]:
    """
    Bounded, deterministic Fruchterman–Reingold-style settling, not physical simulation.

    Repulsion is k²/d; connected nodes attract with d²/k, k=2.5 display units.
    Cooling bounds each displacement. No year enters these forces.
    """
    radius = 2.5 * sqrt(len(order)) / 2
    points = {order[0]: (0.0, 0.0)}
    for index, pk in enumerate(order[1:]):
        angle = 2 * pi * index / max(1, len(order) - 1)
        points[pk] = (radius * cos(angle), radius * sin(angle))
    best = _spaced_positions(points)
    best_score = _layout_quality(best, edges)[0] if len(edges) <= 250 else None
    for step in range(80):
        movement = {pk: [0.0, 0.0] for pk in order}
        for a, b in combinations(order, 2):
            dx, dy = points[a][0] - points[b][0], points[a][1] - points[b][1]
            strength = 2.5**2 / max(dx * dx + dy * dy, 1e-9)
            for axis, delta in enumerate((dx * strength, dy * strength)):
                movement[a][axis] += delta
                movement[b][axis] -= delta
        for a, b in edges:
            dx, dy = points[a][0] - points[b][0], points[a][1] - points[b][1]
            strength = hypot(dx, dy) / 2.5
            for axis, delta in enumerate((dx * strength, dy * strength)):
                movement[a][axis] -= delta
                movement[b][axis] += delta
        temperature = 1.2 * (1 - step / 80) + 0.015
        for pk in order:
            dx, dy = movement[pk]
            factor = min(1.0, temperature / max(hypot(dx, dy), 1e-9))
            points[pk] = (points[pk][0] + dx * factor,
                          points[pk][1] + dy * factor)
        if best_score is not None and step % 10 == 9:
            candidate = _spaced_positions(points)
            score, _ = _layout_quality(candidate, edges)
            if score < best_score:
                best, best_score = candidate, score
    return best if best_score is not None else _spaced_positions(points)


def _topology_positions(
    order: list[int], neighbour: dict[int, set[int]]
) -> dict[int, tuple[float, float]]:
    """
    A noncrossing tree scaffold is only a candidate; every real cyclic edge is still scored/drawn.
    """
    children = {pk: [] for pk in order}
    depth = {order[0]: 0}
    for pk in order:
        for child in sorted(neighbour[pk]):
            if child not in depth:
                depth[child] = depth[pk] + 1
                children[pk].append(child)
    points = {}
    stack = [(order[0], False)]
    cursor = 0.0
    while stack:
        pk, ready = stack.pop()
        if not ready and children[pk]:
            stack.append((pk, True))
            stack.extend((child, False) for child in reversed(children[pk]))
        else:
            x = (
                fsum(points[child][0]
                     for child in children[pk]) / len(children[pk])
                if children[pk]
                else cursor
            )
            points[pk] = (x, -2.5 * depth[pk])
            if not children[pk]:
                cursor += 2.5
    return points


def _repair_positions(
    points: dict[int, tuple[float, float]], edges: list, neighbour: dict[int, set[int]]
) -> dict[int, tuple[float, float]]:
    """
    Escape crossing-repair local minima, retaining the best lexicographic score.

    Temporary uphill moves are allowed during cooling, but the returned result
    never worsens crossings/clearance. Work is bounded by graph size; this is a
    deterministic heuristic, not a claim of an optimal drawing for every graph.
    """
    best_score, trouble = _layout_quality(points, edges)
    if best_score[:2] == (0, 0):
        return points
    best = dict(points)
    score = best_score
    rng = Random(7)
    work = len(points) ** 2 + len(edges) ** 2 + len(points) * len(edges)
    trials = max(16, min(2400, 4_000_000 // max(1, work)))
    for step in range(trials):
        pk = rng.choice(sorted(trouble or points))
        adjacent = sorted(neighbour[pk])
        cx = fsum(points[other][0] for other in adjacent) / len(adjacent)
        cy = fsum(points[other][1] for other in adjacent) / len(adjacent)
        clear = best_score[:2] == (0, 0)
        if clear:
            # Once readable, compact within the same planar drawing; never trade crossings for size.
            cx = points[pk][0] * 0.75 + cx * 0.25
            cy = points[pk][1] * 0.75 + cy * 0.25
        radius = 0.8 if clear else max(
            2.5, sqrt(score[2] / max(1, len(edges))))
        angle = rng.random() * 2 * pi
        distance = radius * rng.uniform(0.3, 1.5)
        candidate = dict(points)
        candidate[pk] = (cx + distance * cos(angle),
                         cy + distance * sin(angle))
        try:
            candidate = _spaced_positions(candidate, shrink=clear)
        except ValueError:
            continue
        candidate_score, candidate_trouble = _layout_quality(candidate, edges)
        if candidate_score < best_score:
            best, best_score = candidate, candidate_score
            if best_score[:2] == (0, 0):
                points, score, trouble = best, best_score, candidate_trouble
                continue
        if clear:
            continue
        energy = score[0] * 1000 + score[1] * 20 + score[2] * 0.01
        candidate_energy = (
            candidate_score[0] * 1000 + candidate_score[1] *
            20 + candidate_score[2] * 0.01
        )
        temperature = 1400 * (1 - step / trials) ** 2 + 0.1
        if candidate_energy <= energy or rng.random() < exp(
            min(0.0, (energy - candidate_energy) / temperature)
        ):
            points, score, trouble = candidate, candidate_score, candidate_trouble
    return best


def family_positions(
    nodes: list[dict], edges: list
) -> tuple[dict[int, tuple[float, float]], set[int]]:
    """
    Space and pack citation clusters; chronology only chooses a rigid orientation.

    Crossing/clearance scores outrank compactness. Rotating a completed group
    towards a loose year trend cannot change its crossings or node spacing.
    Returned starting-paper IDs retain the existing newest-selected semantics.
    """
    by_id = {n["id"]: n for n in nodes}
    neighbour: dict[int, set[int]] = {pk: set() for pk in by_id}
    for src, tgt in edges:
        if src in neighbour and tgt in neighbour and src != tgt:
            neighbour[src].add(tgt)
            neighbour[tgt].add(src)
    links = sorted({tuple(sorted((a, b)))
                   for a in neighbour for b in neighbour[a]})
    unseen = set(by_id)
    clusters = []
    roots: set[int] = set()
    for start in sorted(by_id):
        if start not in unseen:
            continue
        stack = [start]
        members = set()
        while stack:
            pk = stack.pop()
            if pk not in unseen:
                continue
            unseen.remove(pk)
            members.add(pk)
            stack.extend(sorted(neighbour[pk] & unseen, reverse=True))
        selected = [pk for pk in members if by_id[pk].get(
            "is_seed")] or list(members)
        dated_selected = [
            pk for pk in selected if by_id[pk].get("year") is not None]
        if dated_selected:
            newest_year = max(by_id[pk]["year"] for pk in dated_selected)
            roots.update(
                pk for pk in dated_selected if by_id[pk]["year"] == newest_year)
        else:
            roots.add(min(selected))
        hub = min(members, key=lambda pk: (-len(neighbour[pk]), pk))
        queue = deque([hub])
        order, visited = [], {hub}
        while queue:
            pk = queue.popleft()
            order.append(pk)
            for other in sorted(neighbour[pk]):
                if other not in visited:
                    visited.add(other)
                    queue.append(other)
        component_edges = [(a, b) for a, b in links if a in members]
        # ponytail: bound settling to 200 nodes/250 edges; use a spaced topology grid beyond that.
        # A specialised large-graph layouter is the upgrade path, not unbounded request-time search.
        if len(order) > 200 or len(component_edges) > 250:
            columns = ceil(sqrt(len(order) * 1.6))
            points = {
                pk: (
                    2.5
                    * (
                        index % columns
                        if (index // columns) % 2 == 0
                        else columns - 1 - index % columns
                    ),
                    -2.5 * (index // columns),
                )
                for index, pk in enumerate(order)
            }
        else:
            random_order = sorted(members)
            Random(0).shuffle(random_order)
            points = _spaced_positions(_topology_positions(order, neighbour))
            best_score = (
                _layout_quality(points, component_edges)[0] if len(
                    component_edges) <= 250 else None
            )
            for candidate_order in (order, sorted(members), random_order):
                candidate = _spring_positions(candidate_order, component_edges)
                score, trouble = (
                    _layout_quality(candidate, component_edges)
                    if len(component_edges) <= 250
                    else ((0, 0, 0.0), set())
                )
                if best_score is None or score < best_score:
                    points, best_score = candidate, score
            if len(component_edges) <= 250:
                # Bounded coordinate exchanges repair crossings without forcing a false tree.
                score, trouble = _layout_quality(points, component_edges)
                critical = sorted(
                    trouble, key=lambda pk: (-len(neighbour[pk]), pk))
                for a, b in islice(combinations(critical, 2), 24):
                    candidate = dict(points)
                    candidate[a], candidate[b] = candidate[b], candidate[a]
                    new_score, _ = _layout_quality(candidate, component_edges)
                    if new_score < score:
                        points, score = candidate, new_score
                _, trouble = _layout_quality(points, component_edges)
                for pk in sorted(trouble, key=lambda pk: (-len(neighbour[pk]), pk))[:6]:
                    for dx, dy in ((0.83, 0.63), (0.83, -0.63), (-0.83, 0.63), (-0.83, -0.63)):
                        candidate = dict(points)
                        candidate[pk] = (points[pk][0] + dx,
                                         points[pk][1] + dy)
                        try:
                            candidate = _spaced_positions(candidate)
                        except ValueError:
                            continue
                        new_score, _ = _layout_quality(
                            candidate, component_edges)
                        if new_score < score:
                            points, score = candidate, new_score
                points = _repair_positions(points, component_edges, neighbour)
        # A rigid rotation aligns the overall year trend; individual papers may still be out of order.
        dated = [pk for pk in order if by_id[pk].get("year")]
        if len(dated) > 1:
            mean_year = fsum(by_id[pk]["year"] for pk in dated) / len(dated)
            vx = fsum((by_id[pk]["year"] - mean_year)
                      * points[pk][0] for pk in dated)
            vy = fsum((by_id[pk]["year"] - mean_year)
                      * points[pk][1] for pk in dated)
            norm = hypot(vx, vy)
            if norm > 1e-9:
                # A diagonal trend uses both plot dimensions instead of turning citation chains into thin columns.
                points = {
                    pk: (
                        ((vy + vx) * x + (vy - vx) * y) / (norm * sqrt(2)),
                        ((vx - vy) * x + (vx + vy) * y) / (norm * sqrt(2)),
                    )
                    for pk, (x, y) in points.items()
                }
        xmin, xmax = min(x for x, _ in points.values()), max(
            x for x, _ in points.values())
        ymin, ymax = min(y for _, y in points.values()), max(
            y for _, y in points.values())
        clusters.append((points, xmin, xmax, ymin, ymax))
    # Pack disconnected groups in shelves, rather than one extremely long strip of tiny clusters.
    target = max((xmax - xmin + 2.5 for _, xmin,
                 xmax, _, _ in clusters), default=0)
    target = max(
        target,
        sqrt(
            fsum(
                (xmax - xmin + 4.5) * (ymax - ymin + 4.5) for _, xmin, xmax, ymin, ymax in clusters
            )
            * 1.6
        ),
    )
    positions: dict[int, tuple[float, float]] = {}
    cursor = row_y = row_height = 0.0
    for points, xmin, xmax, ymin, ymax in sorted(
        clusters, key=lambda c: (-(c[2] - c[1] + 2.5)
                                 * (c[4] - c[3] + 2.5), min(c[0]))
    ):
        width, height = xmax - xmin + 2.5, ymax - ymin + 2.5
        if cursor and cursor + width > target:
            cursor = 0.0
            row_y += row_height + 2.0
            row_height = 0.0
        for pk, (x, y) in points.items():
            positions[pk] = (x - xmin + cursor + 1.25, y - ymax - row_y - 1.25)
        cursor += width + 2.0
        row_height = max(row_height, height)
    return positions, roots


# --- Citation stories ---
def build_citation_figure(
    graph: dict, *, include_app_script: bool = True,
    plot_config: dict | None = None,
) -> str:
    """
    Interactive Plotly citation clusters with directed edge tooltips.

    Node traces keep their existing customdata HTML hover text untouched and
    additionally carry raw per-node dicts in meta.paper_details (aligned with
    pointNumber) for the shared graph_node_popup.js renderer. Positions follow
    citation topology; chronology is only a soft orientation preference.
    The bounded canvas starts at the same full-graph extent as Reset view;
    wheel zoom and panning allow closer inspection without an oversized canvas.
    Standalone exports pass include_app_script=False: the app-only popup
    script needs the running app, so exports keep pan/zoom/hover/download
    and label list links as app-only.

    Args:
        graph: Existing graph nodes, directed edges and display metadata.

    Kwargs:
        include_app_script: Include local node-popup script. Defaults to True.
        plot_config: Plotly configuration overrides. Defaults to None.

    Returns:
        str: Embeddable graph HTML preserving every supplied edge and cycle.
    """
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        fig = go.Figure()
        fig.update_layout(
            height=700, autosize=True,
            xaxis={"visible": False, "fixedrange": True},
            yaxis={"visible": False, "fixedrange": True},
        )
        return fig.to_html(full_html=False, include_plotlyjs=False,
                           config={"responsive": True, **(plot_config or {})})

    by_id = {n["id"]: n for n in nodes}
    positions, roots = family_positions(nodes, edges)
    # 40px/unit makes the 0.8-unit edge clearance 32px from a node centre:
    # at least ~8px beyond the largest 46px circle and strokes at readable zoom.
    # ponytail: zoom the view, not the canvas; keep every node and link.
    plot_width, plot_height = 720, 700
    viewport = {}
    axes = {}
    for name, coordinates, pixels in (
        ("xaxis", [x for x, _ in positions.values()], plot_width - 80),
        ("yaxis", [y for _, y in positions.values()], plot_height - 180),
    ):
        low, high = min(coordinates), max(coordinates)
        # At full fit, allow 32px for the largest 46px circle and its stroke.
        padding = max(2, (high - low) * 32 / (pixels - 64))
        fit = [low - padding, high + padding]
        middle = (fit[0] + fit[1]) / 2
        half_span = min(fit[1] - fit[0], pixels / 40) / 2
        initial = [middle - half_span, middle + half_span]
        # Reserve a small margin outside the full fit: pan and zoom-out
        # remain usable even when the whole graph first fits the canvas.
        margin = (fit[1] - fit[0]) * 0.2
        bounds = [fit[0] - margin, fit[1] + margin]
        viewport[name] = {"bounds": bounds, "initial": initial, "fit": fit}
        axes[name] = {"visible": False, "fixedrange": False, "range": fit[:],
                      "autorange": False}
        # Older Plotly versions lack native bounds; the inline clamp applies.
        if "minallowed" in go.layout.XAxis()._valid_props:
            axes[name].update(minallowed=bounds[0], maxallowed=bounds[1])
            axes[name]["autorangeoptions"] = {
                "minallowed": bounds[0], "maxallowed": bounds[1]}

    inner_x: list[float | None] = []
    inner_y: list[float | None] = []
    outer_x: list[float | None] = []
    outer_y: list[float | None] = []
    hover_x: list[float] = []
    hover_y: list[float] = []
    hover_text: list[str] = []
    for src, tgt in edges:
        if src in positions and tgt in positions:
            x1, y1 = positions[src]
            x2, y2 = positions[tgt]
            if (by_id[src].get("is_seed") or by_id[src].get("is_joiner")) and (
                by_id[tgt].get("is_seed") or by_id[tgt].get("is_joiner")
            ):
                inner_x += [x1, x2, None]
                inner_y += [y1, y2, None]
            else:
                outer_x += [x1, x2, None]
                outer_y += [y1, y2, None]
            hover_x.append((x1 + x2) / 2)
            hover_y.append((y1 + y2) / 2)
            hover_text.append(
                f"{escape(str(by_id[src]['title']))} cites "
                f"{escape(str(by_id[tgt]['title']))}"
            )

    fig = go.Figure()
    for xs, ys, color, dash in (
        (outer_x, outer_y, "#c1bbeb", "dot"),
        (inner_x, inner_y, "#677b68", "solid"),
    ):
        if xs:
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines",
                line=dict(color=color, width=1.6, dash=dash),
                hoverinfo="skip", showlegend=False,
            ))
    if hover_x:
        fig.add_trace(go.Scatter(
            x=hover_x, y=hover_y, mode="markers",
            marker=dict(size=7, color="#9aa4af"),
            text=hover_text, hovertemplate="%{text}<extra></extra>",
            name="Citation direction: citing → cited", showlegend=False,
        ))
    max_cites = max((n["citations"] or 0) for n in nodes) or 1

    def _role(node: dict) -> str:
        """
        Choose the existing graph color role without changing membership.

        Args:
            node: Graph node with seed and joining-path flags.

        Kwargs:
            None

        Returns:
            str: seed, joiner or outer display role, in that priority order.
        """
        if node.get("is_seed"):
            return "seed"
        if node.get("is_joiner"):
            return "joiner"
        return "outer"

    for label, role, color in (
        ("Your chosen survey entries" if graph.get("curated")
         else "Your kept papers", "seed", "#3f7e3f"),
        ("Links between kept papers", "joiner", "#8a8a8a"),
        ("Unconnected branches", "outer", "#3C27F5"),
    ):
        xs: list[float] = []
        ys: list[float] = []
        sizes: list[float] = []
        texts: list[str] = []
        years_inside: list[str] = []
        details: list[dict] = []
        for node in nodes:
            if _role(node) != role:
                continue
            x, y = positions[node["id"]]
            xs.append(x)
            ys.append(y)
            # Citation counts are heavy-tailed: a bounded log scale keeps
            # the largest paper from obscuring its neighbours.
            sizes.append(
                28 + 18 * log1p(max(0, node["citations"] or 0)) / log1p(max_cites))
            star = " ★ Starting paper" if node["id"] in roots else ""
            texts.append(
                f"{escape(str(node['title']))}{star}<br>Year: {node['year'] or 'unknown'}"
                f"<br>Cited by: {node['citations']}<br>Venue: {escape(str(node['venue'] or 'unknown'))}"
            )
            circle_text = str(node["year"]) if node["year"] else "?"
            if node.get("reference_number"):
                circle_text += f"<br>[{node['reference_number']}]"
            years_inside.append(circle_text)
            details.append(
                {
                    "title": str(node.get("title") or ""),
                    "year": node.get("year"),
                    "venue": str(node.get("venue") or ""),
                    "citations": int(node.get("citations") or 0),
                    "ref": node.get("reference_number"),
                }
            )
        if not xs:
            continue
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text",
                name=f"{label} ({len(xs)})",
                textposition="middle center",
                textfont=dict(color="#15202a" if role ==
                              "joiner" else "#ffffff", size=10),
                marker=dict(
                    size=sizes,
                    color=color,
                    line=dict(width=1, color="#333333"),
                ),
                text=years_inside,
                customdata=texts,
                hovertemplate="%{customdata}<extra></extra>",
                meta={"paper_details": details},
            )
        )
    if not edges:
        fig.add_annotation(
            text="No matched citations between these project papers yet.",
            xref="paper", yref="paper", x=0.5, y=1.08,
            showarrow=False, font=dict(color="#555555"),
        )
    smallest_node_diameter = min(
        size for trace in fig.data if trace.meta and "paper_details" in trace.meta
        for size in trace.marker.size
    )
    fig.update_layout(
        title=graph.get("title", "How your kept papers link together") +
        " (readability-first layout)",
        height=plot_height, autosize=True,
        **axes,
        meta={"citation_viewport": viewport,
              "smallest_node_diameter": smallest_node_diameter},
        legend_title="Dot colour",
        legend={"orientation": "h", "x": 0, "y": 0,
                "xanchor": "left", "yanchor": "top"},
        dragmode="pan",
        margin={"l": 40, "r": 40, "t": 80, "b": 100},
        hovermode="closest",
    )
    from django.templatetags.static import static

    # ponytail: inline the same tiny controller for app pages and offline exports.
    viewport_script = (
        Path(__file__).resolve().parents[1] /
        "static/literature/citation_viewport.js"
    ).read_text(encoding="utf-8")
    html = fig.to_html(full_html=False, include_plotlyjs=False,
                       config={"responsive": True, "scrollZoom": True,
                               **(plot_config or {})},
                       post_script=viewport_script + "\nwindow.dispatchEvent(new CustomEvent('graph-ready', {detail: '{plot_id}'}));")
    popup = ""
    if include_app_script:
        popup_script = static("literature/graph_node_popup.js")
        popup = f'<script src="{escape(popup_script)}" defer></script>'
    return (
        '<div role="region" aria-label="Citation graph, scroll horizontally" '
        'aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown Home" '
        'title="Scroll to zoom; drag to pan. Keyboard: + or - to zoom, '
        'arrows to pan, Home to fit the whole graph." '
        'tabindex="0" style="max-width:100%;overflow-x:auto">'
        f'<div style="width:100%;min-width:{plot_width}px">{html}</div></div>'
        f'{popup}'
    )
