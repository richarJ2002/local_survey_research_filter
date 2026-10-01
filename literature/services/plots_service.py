#!/usr/bin/env python3

'''
@File:   plots_service.py

@Brief:  Plotly figures for the project analysis section.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

from html import escape
from math import log1p

# Custom library Imports
import plotly.graph_objects as go

# ------------------------------------------------------------------------ #
# Keyword hits figure
# ------------------------------------------------------------------------ #

# --- Keyword funnel bars ---
def build_keyword_figure(counts: dict[str, dict[str, int]]) -> str:
    """Grouped bars per keyword: Found vs Title Yes vs Abstract Yes.

    Plain English labels; muted colours; decreasing heights show filtering.
    Returns an embeddable HTML div (Plotly.js via CDN).
    """
    keywords = list(counts.keys())
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
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


# ------------------------------------------------------------------------ #
# Source pie / funnel / time plots
# ------------------------------------------------------------------------ #

# --- Where raw records and removals came from ---
def source_pie_figure(
    per_source: dict[str, dict[str, int]], *, metric: str = "results",
) -> go.Figure:
    """Show each source's share of one audited category.

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
        "google_scholar": "Google Scholar",
        "publisher": "Publisher / journal website",
        "reference_list": "Reference list",
        "other": "Other source",
        "manual": "Unspecified origin (legacy)",
    }
    colors = {
        "openalex": "#4c78a8",
        "semantic_scholar": "#e5a33d",
        "crossref": "#8064a6",
        "arxiv": "#3f9d91",
        "manual": "#64748b",
    }
    ordered = [key for key in names if key in per_source]
    ordered.extend(sorted(set(per_source) - set(names)))
    values = [max(0, int(per_source[key].get(metric, 0) or 0)) for key in ordered]
    total = sum(values)
    fig = go.Figure()
    if total:
        rates = [
            100 * value / raw if (raw := per_source[key].get("results", 0)) else 0
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
            marker=dict(colors=[colors.get(key, "#718096") for key in ordered]),
            customdata=rates,
            textinfo="label+percent",
            hovertemplate=hover + "<extra></extra>",
        ))
    else:
        fig.add_annotation(
            text="No records in this category yet", x=0.5, y=0.5,
            xref="paper", yref="paper", showarrow=False,
        )
    fig.update_layout(title=f"{titles[metric]} ({total} total)", height=440,
                      margin=dict(l=40, r=40, t=80, b=40))
    return fig


def build_source_pie(
    per_source: dict[str, dict[str, int]], *, metric: str = "results",
) -> str:
    """One embeddable Plotly pie with its own camera download button."""
    fig = source_pie_figure(per_source, metric=metric)
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


# --- Filtering flow (PRISMA-style Sankey, left to right) ---
def build_funnel_figure(metrics: dict) -> str:
    """Left-to-right flow showing how papers split at each check.

    Dropped lanes sit above, accepted lanes at the bottom. Node labels
    stay short (Plotly draws rightmost labels over the bands, so long
    text overlaps); counts live on the bands themselves via link labels
    plus hover text.
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
    values = [dups, unique, title_no, title_yes, stages.get("Title", {}).get("pending", 0), abstract_no, abstract_yes, stages.get("Abstract", {}).get("pending", 0)]
    values = [max(int(v or 0), 0) for v in values]
    if sum(values) == 0:
        values = [0] * len(sources)
    fig = go.Figure(
        go.Sankey(
            arrangement="snap",
            node=dict(
                label=labels,
                pad=20,
                color=["#8aa8c8", "#bbbbbb", "#9dbf9d", "#c88a8a", "#6f9e6f", "#a85a5a", "#3f7e3f", "#c4b78c", "#c4b78c"],
            ),
            link=dict(
                source=sources,
                target=targets,
                value=values,
                label=[str(v) for v in values],
                hovertemplate="From %{source.label} to %{target.label}: %{value} papers<extra></extra>",
            ),
        )
    )
    fig.update_layout(
        title="How papers flow through each check (left to right)",
        font=dict(size=12),
    )
    if metrics.get("title_not_queued", 0):
        fig.add_annotation(text=f"{metrics['title_not_queued']} stored papers are not queued for Title screening.<br>They are not pending decisions or rejections; missing titles need completion.", xref="paper", yref="paper", x=0, y=0, yshift=-70, xanchor="left", yanchor="top", showarrow=False)
        fig.update_layout(margin={"b": 120})
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


# --- Papers per year ---
def build_yearly_figure(data) -> str:
    """Bars per year, layered by step (largest behind).

    Accepts the stage_year_cumulative dict (preferred) or a plain year
    list (single layer, backward compatible). The ring left showing
    around a colour = papers from that year lost at that check.
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
        for trace, step in zip(fig.data, ("found", "title", "abstract")):
            trace.customdata = [[step, "year", year] for year in years]
        return fig.to_html(full_html=False, include_plotlyjs="cdn", post_script="window.dispatchEvent(new CustomEvent('survey-chart-ready', {detail: '{plot_id}'}));")
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
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


# ------------------------------------------------------------------------ #
# Cumulative by stage over years
# ------------------------------------------------------------------------ #

# --- Stage running totals ---
def build_stage_cumulative_figure(data: dict) -> str:
    """Running totals per year, one line per filter step.

    Gaps between lines in an era = papers from those years filtered out.
    Dropdown isolates one step or compares all.
    A box plot would not fit here (boxes show spread, not growth over time).
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
        return fig.to_html(full_html=False, include_plotlyjs="cdn")

    def _trace(name: str, values: list[int], color: str) -> go.Scatter:
        return go.Scatter(
            x=years,
            y=values,
            mode="lines+markers",
            name=name,
            line=dict(color=color),
            hovertemplate="Year: %{x}<br>" + name + ": %{y} so far<extra></extra>",
        )

    fig = go.Figure()
    fig.add_trace(_trace("Found papers", series.get("found", []), "#8aa8c8"))
    fig.add_trace(_trace("Kept after title check", series.get("title", []), "#6f9e6f"))
    fig.add_trace(_trace("Kept after abstract check", series.get("abstract", []), "#c8a06f"))
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
                    dict(label="Compare: all steps", method="update", args=[{"visible": [True, True, True]}]),
                    dict(label="Found only", method="update", args=[{"visible": [True, False, False]}]),
                    dict(label="Title-kept only", method="update", args=[{"visible": [False, True, False]}]),
                    dict(label="Abstract-kept only", method="update", args=[{"visible": [False, False, True]}]),
                ],
            ),
        ],
    )
    fig.add_annotation(**footer)
    fig.update_layout(margin={"b": 180})
    for trace, step in zip(fig.data, ("found", "title", "abstract")):
        trace.customdata = [[step, "through", year] for year in years]
    return fig.to_html(full_html=False, include_plotlyjs="cdn", post_script="window.dispatchEvent(new CustomEvent('survey-chart-ready', {detail: '{plot_id}'}));")


# ------------------------------------------------------------------------ #
# Citation network (2D)
# ------------------------------------------------------------------------ #

# --- Backward citation positions ---
def family_positions(nodes: list[dict], edges: list) -> tuple[dict[int, tuple[float, float]], set[int]]:
    """Lay out clusters with newer papers above older ones and fewer crossings.

    Year ranks are evenly spaced rather than plotted on a calendar axis.
    Repeated barycentric sweeps group linked neighbours; for modest graphs
    keep the best layout measured by straight-line edge intersections.
    """
    neighbour: dict[int, set[int]] = {n["id"]: set() for n in nodes}
    for src, tgt in edges:
        if src in neighbour and tgt in neighbour:
            neighbour[src].add(tgt)
            neighbour[tgt].add(src)
    component_of: dict[int, int] = {}
    components: list[set[int]] = []
    for node in sorted(nodes, key=lambda n: n["id"]):
        if node["id"] in component_of:
            continue
        stack = [node["id"]]
        current: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in component_of:
                continue
            component_of[cur] = len(components)
            current.add(cur)
            stack.extend(sorted(neighbour.get(cur, ()) - set(component_of), reverse=True))
        components.append(current)
    by_id = {n["id"]: n for n in nodes}
    dated = [n["year"] for n in nodes if n["year"]]
    undated_row = min(dated) - 1 if dated else 0
    # Roots: newest selected paper(s) of each component. An undated-only
    # cluster gets a stable starting paper without inventing a year.
    roots: set[int] = set()
    for members in components:
        selected = [pk for pk in members if by_id[pk].get("is_seed")] or list(members)
        dated_selected = [pk for pk in selected if by_id[pk]["year"] is not None]
        if dated_selected:
            newest_year = max(by_id[pk]["year"] for pk in dated_selected)
            roots.update(pk for pk in dated_selected if by_id[pk]["year"] == newest_year)
        else:
            roots.add(min(selected))
    positions: dict[int, tuple[float, float]] = {}
    offset = 0.0
    spacing = 2.5
    for members in components:
        rows: dict[int, list[int]] = {}
        for pk in members:
            rows.setdefault(by_id[pk]["year"] or undated_row, []).append(pk)
        years = sorted(rows, reverse=True)
        order = {year: sorted(rows[year]) for year in years}
        width = max(len(row) for row in rows.values())

        def slots() -> dict[int, float]:
            return {
                pk: (width - len(row)) / 2 + i
                for row in order.values() for i, pk in enumerate(row)
            }

        component_edges = [(src, tgt) for src, tgt in edges if src in members and tgt in members]
        rank = {year: float(len(years) - i) for i, year in enumerate(years)}

        def crossing_count() -> int:
            x = slots()
            points = {pk: (x[pk], rank[by_id[pk]["year"] or undated_row]) for pk in members}

            def turn(a: int, b: int, c: int) -> float:
                ax, ay = points[a]
                bx, by = points[b]
                cx, cy = points[c]
                return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

            count = 0
            for i, (a, b) in enumerate(component_edges):
                for c, d in component_edges[i + 1:]:
                    if len({a, b, c, d}) == 4 and (
                        turn(a, b, c) * turn(a, b, d) < 0
                        and turn(c, d, a) * turn(c, d, b) < 0
                    ):
                        count += 1
            return count

        score_layout = len(component_edges) <= 250
        best = {year: list(row) for year, row in order.items()}
        best_crossings = crossing_count() if score_layout else 0
        for _ in range(4):
            for sweep in (years[1:], list(reversed(years[:-1]))):
                for year in sweep:
                    x = slots()
                    previous = {pk: i for i, pk in enumerate(order[year])}

                    def barycenter(pk: int) -> tuple[float, int, int]:
                        linked = [x[other] for other in neighbour[pk]
                                  if other in x and (by_id[other]["year"] or undated_row) != year]
                        return (sum(linked) / len(linked) if linked else x[pk],
                                previous[pk], pk)

                    order[year].sort(key=barycenter)
            if score_layout:
                crossings = crossing_count()
                if crossings < best_crossings:
                    best_crossings = crossings
                    best = {year: list(row) for year, row in order.items()}
        if score_layout:
            order = best
        for year, row in order.items():
            for i, pk in enumerate(row):
                positions[pk] = (offset + ((width - len(row)) / 2 + i) * spacing,
                                 rank[year])
        offset += width * spacing + 6
    return positions, roots


# --- Citation stories ---
def build_citation_figure(graph: dict) -> str:
    """Interactive Plotly citation clusters with directed edge tooltips."""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        fig = go.Figure()
        fig.update_layout(
            title=graph.get("title", "How your kept papers link together") + " (no papers to show yet)",
            height=700,
        )
        return fig.to_html(full_html=False, include_plotlyjs="cdn")

    by_id = {n["id"]: n for n in nodes}
    positions, roots = family_positions(nodes, edges)
    horizontal_span = max(x for x, _ in positions.values()) - min(
        x for x, _ in positions.values()
    )
    plot_width = max(1100, min(20000, int((horizontal_span + 4) * 44)))

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
        if node.get("is_seed"):
            return "seed"
        if node.get("is_joiner"):
            return "joiner"
        return "outer"

    for label, role, color in (
        ("Your kept papers", "seed", "#3f7e3f"),
        ("Links between kept papers", "joiner", "#8a8a8a"),
        ("Unconnected branches", "outer", "#3C27F5"),
    ):
        xs: list[float] = []
        ys: list[float] = []
        sizes: list[float] = []
        texts: list[str] = []
        years_inside: list[str] = []
        for node in nodes:
            if _role(node) != role:
                continue
            x, y = positions[node["id"]]
            xs.append(x)
            ys.append(y)
            # Citation counts are heavy-tailed: a bounded log scale keeps
            # the largest paper from obscuring its neighbours.
            sizes.append(28 + 18 * log1p(max(0, node["citations"] or 0)) / log1p(max_cites))
            star = " ★ Starting paper" if node["id"] in roots else ""
            texts.append(
                f"{escape(str(node['title']))}{star}<br>Year: {node['year'] or 'unknown'}"
                f"<br>Cited by: {node['citations']}<br>Venue: {escape(str(node['venue'] or 'unknown'))}"
            )
            circle_text = str(node["year"]) if node["year"] else "?"
            if node.get("reference_number"):
                circle_text += f"<br>[{node['reference_number']}]"
            years_inside.append(circle_text)
        if not xs:
            continue
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text",
                name=f"{label} ({len(xs)})",
                textposition="middle center",
                textfont=dict(color="#15202a" if role == "joiner" else "#ffffff", size=10),
                marker=dict(
                    size=sizes,
                    color=color,
                    line=dict(width=1, color="#333333"),
                ),
                text=years_inside,
                customdata=texts,
                hovertemplate="%{customdata}<extra></extra>",
            )
        )
    if not edges:
        fig.add_annotation(
            text="No matched citations between these project papers yet.",
            xref="paper", yref="paper", x=0.5, y=1.08,
            showarrow=False, font=dict(color="#555555"),
        )
    years = sorted({n["year"] for n in nodes if n["year"]})
    if any(not n["year"] for n in nodes):
        years = [(years[0] - 1) if years else 0] + years
    fig.update_layout(
        title=graph.get("title", "How your kept papers link together") + " (newer above, older below)",
        height=max(750, min(2200, 450 + 70 * len(years))),
        width=plot_width,
        xaxis=dict(visible=False, fixedrange=False),
        yaxis=dict(visible=False, fixedrange=False),
        legend_title="Dot colour",
        dragmode="pan",
        margin=dict(l=90, r=30, t=80, b=40),
        hovermode="closest",
    )
    html = fig.to_html(full_html=False, include_plotlyjs="cdn",
                       config={"responsive": False, "scrollZoom": True})
    return (
        '<div role="region" aria-label="Citation graph, scroll horizontally" '
        'tabindex="0" style="max-width:100%;overflow-x:auto">'
        f"{html}</div>"
    )
