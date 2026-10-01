#!/usr/bin/env python3

'''
@File:   opencitations_service.py

@Brief:  Explicit, bounded DOI citation lookups cached for project graphs.

@Date:   30/09/2026

@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.
'''

from __future__ import annotations

import re
import time
from datetime import timedelta
from urllib.parse import quote

import httpx
from django.conf import settings
from django.utils import timezone

from literature.models import Discovery, OpenCitationsLookup, Paper, ScreeningDecision
from literature.services.reference_service import normalize_doi

# ------------------------------------------------------------------------ #
# OpenCitations Index v2: no network access from graph rendering
# ------------------------------------------------------------------------ #

BASE_URL = "https://api.opencitations.net/index/v2"
MAX_RESPONSE_BYTES = 2_000_000
MAX_REQUESTS = 20
CACHE_DAYS = 30
REQUEST_INTERVAL = 0.4  # At most 150 calls/minute, below the 180/minute API cap.
DOI_PATTERN = re.compile(r"(?:^|[\s;])doi:([^\s;]+)", re.IGNORECASE)
DIRECTIONS = (("outgoing", "references"), ("incoming", "citations"))


class OpenCitationsError(RuntimeError):
    """One DOI lookup could not be completed."""


class OpenCitationsRateLimit(OpenCitationsError):
    """Stop this batch rather than repeatedly hitting a shared limit."""


def _dois(value: object) -> list[str]:
    """Extract DOI identifiers from a space/semicolon-separated PID field."""
    if not isinstance(value, str):
        return []
    return [doi for match in DOI_PATTERN.findall(value)
            if (doi := normalize_doi(match))]


def parse_edges(payload: object) -> list[list[str]]:
    """Keep only direct DOI-to-DOI citation pairs in an API response."""
    if not isinstance(payload, list):
        raise OpenCitationsError("Invalid citation response")
    edges: set[tuple[str, str]] = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        for citing in _dois(row.get("citing")):
            for cited in _dois(row.get("cited")):
                if citing != cited:
                    edges.add((citing, cited))
    return [list(pair) for pair in sorted(edges)]


def _fetch(doi: str, endpoint: str, client: httpx.Client) -> list[list[str]]:
    """Fetch one direction using a fixed host, with bounded JSON response."""
    url = f"{BASE_URL}/{endpoint}/doi:{quote(doi, safe='')}"
    response = client.get(url, timeout=20.0)
    if response.status_code == 429:
        raise OpenCitationsRateLimit("OpenCitations rate limited")
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        raise OpenCitationsError(f"OpenCitations returned HTTP {response.status_code}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise OpenCitationsError("OpenCitations response exceeded size limit")
    return parse_edges(response.json())


def enrich_project(
    project, *, max_requests: int = MAX_REQUESTS, client: httpx.Client | None = None,
) -> dict[str, int | bool]:
    """Fetch the next uncached DOI lookups for project papers (POST only).

    Successful empty results are cached too. Failed requests are not cached,
    so a later explicit retry can recover without losing a citation link.
    """
    paper_ids = set(Discovery.objects.filter(search__project=project).values_list(
        "paper_id", flat=True,
    ))
    accepted = set(ScreeningDecision.objects.filter(
        stage__project=project, decision="ACCEPT",
    ).values_list("paper_id", flat=True))
    papers = sorted(Paper.objects.filter(pk__in=paper_ids).only("id", "doi"),
                    key=lambda paper: (paper.pk not in accepted, paper.pk))
    dois = list(dict.fromkeys(doi for paper in papers if (doi := normalize_doi(paper.doi))))
    if not dois:
        return {"checked": 0, "cached": 0, "matched": 0, "failed": 0,
                "remaining": 0, "rate_limited": False}

    cached = {(lookup.doi, lookup.direction): lookup for lookup in
              OpenCitationsLookup.objects.filter(doi__in=dois)}
    fresh_after = timezone.now() - timedelta(days=CACHE_DAYS)
    todo: list[tuple[str, str, str]] = []
    fresh_count = 0
    for doi in dois:
        for direction, endpoint in DIRECTIONS:
            entry = cached.get((doi, direction))
            if entry and entry.checked_at >= fresh_after:
                fresh_count += 1
            else:
                todo.append((doi, direction, endpoint))

    own_client = client is None
    token = (getattr(settings, "OPENCITATIONS_TOKEN", "") or "").strip()
    headers = {"Accept": "application/json", "User-Agent": "LocalSurveyResearchFilter/0.1"}
    if token:
        headers["authorization"] = token
    http = client or httpx.Client(headers=headers, follow_redirects=False)
    doi_set = set(dois)
    checked = failed = 0
    matched: set[tuple[str, str]] = set()
    rate_limited = False
    last_request = 0.0
    try:
        for doi, direction, endpoint in todo[:max(0, min(max_requests, MAX_REQUESTS))]:
            wait = REQUEST_INTERVAL - (time.monotonic() - last_request)
            if last_request and wait > 0:
                time.sleep(wait)
            last_request = time.monotonic()
            try:
                edges = _fetch(doi, endpoint, http)
            except OpenCitationsRateLimit:
                rate_limited = True
                failed += 1
                break
            except (OpenCitationsError, httpx.RequestError, ValueError):
                failed += 1
                continue
            OpenCitationsLookup.objects.update_or_create(
                doi=doi, direction=direction, defaults={"edges": edges},
            )
            checked += 1
            matched.update((src, tgt) for src, tgt in edges if src in doi_set and tgt in doi_set)
    finally:
        if own_client:
            http.close()
    return {"checked": checked, "cached": fresh_count, "matched": len(matched),
            "failed": failed, "remaining": len(todo) - checked,
            "rate_limited": rate_limited}
