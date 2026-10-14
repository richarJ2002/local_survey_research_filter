#!/usr/bin/env python3

"""
Preview DOI metadata and fill missing paper fields through provider lookups.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   reference_service.py

@Brief:  Preview DOI metadata and fill missing paper fields through provider lookups.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import random
import time
from urllib.parse import quote

# Custom library Imports
import httpx
from django.conf import settings

from literature.models import Discovery, Paper
from literature.services.screening_service import abstract_available, has_reference_list

# ------------------------------------------------------------------------ #
# Backfill
# ------------------------------------------------------------------------ #

BASE_URL = "https://api.openalex.org/works"
BATCH_SIZE = 50


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Classes
class RefbackfillError(RuntimeError):
    """
    Report an OpenAlex lookup failure or Semantic Scholar rate limit.
    """


# --- Normalize DOI ---
def normalize_doi(raw: str | None) -> str | None:
    """
    Strip whitespace and DOI resolver prefixes and lowercase the result.

    Args:
        raw: DOI string or resolver URL; None is treated as blank.

    Kwargs:
        None

    Returns:
        str | None: Nonblank normalized text or None. This does not validate
            DOI syntax; resolver strings are removed wherever they occur.
    """
    cleaned = (
        (raw or "")
        .strip()
        .lower()
        .replace("https://doi.org/", "")
        .replace("http://doi.org/", "")
    )
    return cleaned or None


# --- Headers ---
def _headers() -> dict:
    """
    Build JSON lookup headers with the configured optional contact email.

    Args:
        None

    Kwargs:
        None

    Returns:
        dict: User-Agent identifies the app and optionally its mailto contact;
            Accept requests application/json. No provider secret is included.
    """
    email = (getattr(settings, "OPENALEX_EMAIL", "") or "").strip()
    agent = "LocalSurveyResearchFilter/0.1"
    if email:
        agent += f" (mailto:{email})"
    return {"User-Agent": agent, "Accept": "application/json"}


# --- Fetch one batch ---
def fetch_batch(dois: list[str], client: httpx.Client) -> dict[str, dict]:
    """
    Fetch canonical preview metadata for an OpenAlex DOI batch.

    Args:
        dois (list[str]): Normalized DOIs joined into one OR filter. Callers
            choose the batch size; this function caps only per-page at 100.
        client (httpx.Client): Borrowed client, normally with lookup headers.

    Kwargs:
        None

    Returns:
        dict: Returned normalized DOIs mapped to dictionaries with title,
            abstract, references (OpenAlex IDs), authors, publication_year,
            venue, openalex_id and source_ids. Unmatched DOIs are absent.
            Skip non-dict items and items lacking DOI text.

    Raises:
        RefbackfillError: If a nonretryable HTTP status occurs or all three
            attempts return 429/5xx.
        TypeError: If the response envelope/results have invalid types, or
            canonical metadata has incompatible types.
        ValueError: If JSON decoding or canonical value parsing fails.
        httpx.RequestError: If HTTP transport fails (not retried here).

    Side Effects:
        GET with a 30-second timeout, retrying 429/5xx up to three attempts
        with exponential jittered sleeps capped at 30 seconds; no DB writes.
    """
    from literature.providers.mapping import map_openalex_to_canonical

    email = (getattr(settings, "OPENALEX_EMAIL", "") or "").strip()
    params: dict = {
        "filter": "doi:" + "|".join(dois),
        "select": "id,doi,title,abstract_inverted_index,referenced_works,authorships,publication_year,primary_location",
        "per-page": min(len(dois), 100),
    }
    if email:
        params["mailto"] = email
    last_status: int | None = None
    for attempt in range(3):
        resp = client.get(BASE_URL, params=params, timeout=30.0)
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_status = resp.status_code
            time.sleep(min((2**attempt) + random.uniform(0, 0.5), 30.0))
            continue
        if resp.status_code != 200:
            raise RefbackfillError(
                f"OpenAlex {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            raise TypeError("OpenAlex returned an invalid works response.")
        mapping: dict[str, dict] = {}
        for item in (payload.get("results") or []):
            if not isinstance(item, dict):
                continue
            doi = normalize_doi(item.get("doi"))
            if doi:
                canonical, _ = map_openalex_to_canonical(item)
                mapping[doi] = {field: canonical[field] for field in
                                ("title", "abstract", "references", "authors", "publication_year", "venue", "openalex_id", "source_ids")}
        return mapping
    raise RefbackfillError(f"OpenAlex retryable {last_status} after 3 tries")


# --- Fill blanks on paper objects ---
def _apply_enrichment(paper: Paper, data: dict) -> bool:
    """
    Fill missing title, usable abstract or reference IDs and save changes.

    Args:
        paper (Paper): Stored paper to update, retaining usable values.
        data (dict): Optional title, abstract and references. The exact
            audience-only placeholder counts as a missing abstract; references
            are filled only when paper.references is falsey.

    Kwargs:
        None

    Returns:
        bool: True if at least one field changed and the paper was saved.

    Side Effects:
        Mutate paper and persist title/abstract/references and updated_at when
        changed. Leave reference_text and all other metadata untouched.
    """
    changed = False
    if not (paper.title or "").strip() and data.get("title"):
        paper.title = data["title"]
        changed = True
    if not abstract_available(paper.abstract) and abstract_available(data.get("abstract")):
        paper.abstract = data["abstract"]
        changed = True
    if not paper.references and data.get("references"):
        paper.references = list(data["references"])
        changed = True
    if changed:
        paper.save(update_fields=["title", "abstract",
                   "references", "updated_at"])
    return changed


# --- DOI fallback providers ---
def _crossref_lookup(doi: str, client: httpx.Client) -> dict:
    """
    Fetch Crossref DOI metadata and DOI-bearing references as a fallback.

    Args:
        doi (str): Normalized DOI to URL-quote in the lookup endpoint.
        client (httpx.Client): Borrowed HTTP client with lookup headers.

    Kwargs:
        None

    Returns:
        dict: title, authors, publication_year, venue and source_ids from
            canonical mapping; abstract with HTML tags stripped; references
            as DOI resolver URLs for DOI-bearing entries. Any non-200 status
            yields {} (no status retry here).

    Raises:
        TypeError: If the response is not a work object or its fields have
            incompatible types for mapping.
        ValueError: If JSON decoding or canonical value parsing fails.
        httpx.RequestError: If HTTP transport fails.

    Side Effects:
        Perform one GET with a 30-second timeout; no database writes.
    """
    from django.utils.html import strip_tags

    from literature.providers.mapping import map_crossref_to_canonical

    response = client.get(
        f"https://api.crossref.org/works/{quote(doi, safe='')}", timeout=30.0,
    )
    if response.status_code != 200:
        return {}
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("message", {}), dict):
        raise TypeError("Crossref returned an invalid work response.")
    item = payload.get("message") or {}
    canonical, _ = map_crossref_to_canonical(item)
    return {
        "title": canonical["title"],
        "authors": canonical["authors"],
        "publication_year": canonical["publication_year"],
        "venue": canonical["venue"],
        "source_ids": canonical["source_ids"],
        "abstract": strip_tags(item.get("abstract") or ""),
        "references": [
            f"https://doi.org/{ref['DOI']}"
            for ref in item.get("reference") or []
            if isinstance(ref, dict) and ref.get("DOI")
        ],
    }


def _s2_lookup(doi: str, client: httpx.Client) -> dict:
    """
    Fetch Semantic Scholar DOI metadata and direct paper references.

    Args:
        doi (str): Normalized DOI to URL-quote in the paper lookup endpoint.
        client (httpx.Client): Borrowed client; per-request headers add the
            configured optional S2 key via x-api-key only.

    Kwargs:
        None

    Returns:
        dict: title, abstract, authors, publication_year, venue and source_ids
            from canonical mapping, plus deduplicated references in encounter
            order. References prefer normalized DOI URLs, then S2:paperId;
            direct paper objects and nested citedPaper entries are supported.
            Non-200 statuses other than 429 yield {}.

    Raises:
        RefbackfillError: If Semantic Scholar returns HTTP 429.
        TypeError: If the response is not a paper object or mapping fields
            have incompatible types.
        ValueError: If JSON decoding or canonical value parsing fails.
        httpx.RequestError: If HTTP transport fails.

    Side Effects:
        Perform one GET with a 30-second timeout; no retries or DB writes.
    """
    from literature.providers.mapping import map_semantic_scholar_to_canonical
    from literature.providers.semantic_scholar import SemanticScholarSource

    response = client.get(
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi, safe='')}",
        params={
            "fields": "title,abstract,authors,year,venue,references.paperId,references.externalIds"},
        headers=SemanticScholarSource().default_headers(),
        timeout=30.0,
    )
    if response.status_code == 429:
        raise RefbackfillError("Semantic Scholar rate limited")
    if response.status_code != 200:
        return {}
    item = response.json()
    if not isinstance(item, dict):
        raise TypeError("Semantic Scholar returned an invalid paper response.")
    refs: list[str] = []
    for entry in item.get("references") or []:
        if not isinstance(entry, dict):
            continue
        # /paper returns direct Paper objects. Accept nested edge fixtures too.
        cited = entry.get("citedPaper") or entry
        if not isinstance(cited, dict):
            continue
        external = cited.get("externalIds") or {}
        if not isinstance(external, dict):
            external = {}
        raw_doi = external.get("DOI")
        cited_doi = normalize_doi(raw_doi) if isinstance(
            raw_doi, str) else None
        if cited_doi:
            refs.append(f"https://doi.org/{cited_doi}")
        elif isinstance(cited.get("paperId"), str) and cited["paperId"]:
            refs.append(f"S2:{cited['paperId']}")
    canonical, _ = map_semantic_scholar_to_canonical(item)
    return {**{field: canonical[field] for field in
               ("title", "abstract", "authors", "publication_year", "venue", "source_ids")},
            "references": list(dict.fromkeys(refs))}


# --- Look up one DOI without writing ---
def lookup_doi(doi: str, client: httpx.Client | None = None) -> dict:
    """
    Merge read-only DOI metadata, retaining the first usable field value.

    No database writes occur. Caught provider transport, shape, parse and
    rate-limit errors are skipped; data from other providers is retained.

    Args:
        doi (str): DOI or resolver URL; must not normalize to blank.

    Kwargs:
        client: Optional borrowed httpx.Client. Defaults to
            None, which creates and closes a redirect-following lookup client.

    Returns:
        dict: Normalized doi and merged title, abstract, references, authors,
            publication_year, venue, openalex_id and source_ids. Try OpenAlex
            first, then Crossref and S2 only while any metadata field is empty;
            source_ids are combined and openalex_id comes only from OpenAlex.
            found is True when title, usable abstract or reference IDs exist,
            not merely when author/year/venue data exists. Missing strings are
            blank, lists empty, year/openalex_id None, and source_ids {}.

    Raises:
        ValueError: If doi normalizes to an empty value.

    Side Effects:
        Make provider requests with OpenAlex retry sleeps as needed, but do
        not modify stored papers. Close only the client created here.
    """
    normalized = normalize_doi(doi)
    if not normalized:
        raise ValueError("Enter a DOI to look up.")
    own_client = client is None
    http = client or httpx.Client(headers=_headers(), follow_redirects=True)
    try:
        merged: dict = {"doi": normalized, "title": "", "abstract": "", "references": [],
                        "authors": [], "publication_year": None, "venue": "", "openalex_id": None,
                        "source_ids": {}}
        fields = ("title", "abstract", "references",
                  "authors", "publication_year", "venue")
        try:
            mapping = fetch_batch([normalized], http)
        except (RefbackfillError, httpx.RequestError, ValueError, TypeError):
            mapping = {}
        data = mapping.get(normalized) or {}
        merged["openalex_id"] = data.get("openalex_id")
        merged["source_ids"].update(data.get("source_ids") or {})
        for field in fields:
            if data.get(field) and (field != "abstract" or abstract_available(data[field])):
                merged[field] = data[field]
        if any(not merged[field] for field in fields):
            try:
                cross = _crossref_lookup(normalized, http)
            except (httpx.RequestError, ValueError, KeyError, TypeError):
                cross = {}
            for field in fields:
                if not merged[field] and cross.get(field) and (field != "abstract" or abstract_available(cross[field])):
                    merged[field] = cross[field]
            merged["source_ids"].update(cross.get("source_ids") or {})
        if any(not merged[field] for field in fields):
            try:
                scholars = _s2_lookup(normalized, http)
            except (RefbackfillError, httpx.RequestError, ValueError, KeyError, TypeError):
                scholars = {}
            for field in fields:
                if not merged[field] and scholars.get(field) and (field != "abstract" or abstract_available(scholars[field])):
                    merged[field] = scholars[field]
            merged["source_ids"].update(scholars.get("source_ids") or {})
        merged["found"] = bool(
            merged["title"] or merged["abstract"] or merged["references"]
        )
        return merged
    finally:
        if own_client:
            http.close()


# --- Enrich a paper list ---
def enrich_papers(
    papers: list, *, batch_size: int = BATCH_SIZE, client: httpx.Client | None = None
) -> int:
    """
    Try OpenAlex, then Crossref and Semantic Scholar for missing fields.

    Only touches papers with a DOI that lack title, abstract, or a reference
    list. A manually pasted bibliography already satisfies that last need.
    Preserve known titles, usable abstracts and existing reference IDs.

    Args:
        papers (list): Stored Paper instances to inspect for missing fields.

    Kwargs:
        batch_size (int): OpenAlex DOI chunk size. Defaults to BATCH_SIZE (50);
            callers should supply a positive value no greater than 100 to
            avoid requesting more DOIs than one response page can return.
        client: Optional borrowed httpx.Client. Defaults to
            None, which creates and closes a redirect-following lookup client.

    Returns:
        int: Number of target instances with at least one field saved, not the
            number of fields or successful requests; zero if no targets exist.

    Raises:
        ValueError: If batch_size is zero while targets exist.
        TypeError: If batch_size is not suitable for range().

    Side Effects:
        Request providers and save eligible title/abstract/reference changes.
        Sleep one second between OpenAlex chunks and at least 1.2 seconds
        between S2 request starts in this call. Skip caught provider errors;
        on S2 rate limiting, stop only S2 lookups for the rest of the run.
    """
    targets = [
        p
        for p in papers
        if normalize_doi(p.doi)
        and (
            not (p.title or "").strip()
            or not abstract_available(p.abstract)
            or not has_reference_list(p)
        )
    ]
    if not targets:
        return 0
    own_client = client is None
    http = client or httpx.Client(headers=_headers(), follow_redirects=True)
    try:
        filled = 0
        batches = 0
        last_s2_request = 0.0
        s2_limited = False
        for start in range(0, len(targets), batch_size):
            chunk = targets[start: start + batch_size]
            if batches:
                time.sleep(1.0)
            try:
                mapping = fetch_batch(
                    [normalize_doi(p.doi) or "" for p in chunk], http
                )
            except (RefbackfillError, httpx.RequestError, ValueError, TypeError):
                # A provider outage must not prevent independent DOI lookups.
                mapping = {}
            batches += 1
            for paper in chunk:
                doi = normalize_doi(paper.doi) or ""
                changed = bool(mapping.get(
                    doi) and _apply_enrichment(paper, mapping[doi]))
                if not has_reference_list(paper) or not (paper.title or "").strip() or not abstract_available(paper.abstract):
                    try:
                        data = _crossref_lookup(doi, http)
                        changed = _apply_enrichment(paper, data) or changed
                    except (httpx.RequestError, ValueError, KeyError, TypeError):
                        pass
                if (not has_reference_list(paper) or not abstract_available(paper.abstract)
                        or not (paper.title or "").strip()) and not s2_limited:
                    # Graph API shared/keyed limits: pace DOI requests, and
                    # stop this provider on rate limiting for the current run.
                    wait = 1.2 - (time.monotonic() - last_s2_request)
                    if last_s2_request and wait > 0:
                        time.sleep(wait)
                    last_s2_request = time.monotonic()
                    try:
                        data = _s2_lookup(doi, http)
                        changed = _apply_enrichment(paper, data) or changed
                    except RefbackfillError:
                        s2_limited = True
                    except (httpx.RequestError, ValueError, KeyError, TypeError):
                        pass
                if changed:
                    filled += 1
        return filled
    finally:
        if own_client:
            http.close()


# --- Backfill project ---
def backfill_missing_references(
    project, *, batch_size: int = BATCH_SIZE, client: httpx.Client | None = None
) -> dict[str, int]:
    """
    Enrich missing fields on DOI papers discovered in a project.

    Args:
        project (Project): Project whose discovered papers are considered.

    Kwargs:
        batch_size (int): OpenAlex DOI chunk size. Defaults to BATCH_SIZE (50);
            supply a positive integer, preferably no greater than 100.
        client: Optional borrowed httpx.Client. Defaults to
            None, which creates and closes a redirect-following lookup client.

    Returns:
        dict: Integer checked counts all discovered papers with a normalized
            nonblank DOI, including already complete papers; filled counts
            targets improved by enrich_papers; batches is the planned OpenAlex
            chunk count for incomplete targets, not a count of HTTP attempts.

    Raises:
        ZeroDivisionError: If batch_size is zero and incomplete targets exist.
        TypeError: If batch_size cannot be used in arithmetic or range().

    Side Effects:
        Read project papers, then delegate provider requests, pacing and
        missing-field writes to enrich_papers; close only a client owned here.
    """
    paper_ids = set(
        Discovery.objects.filter(search__project=project).values_list(
            "paper_id", flat=True
        )
    )
    targets = list(
        Paper.objects.filter(pk__in=paper_ids).only(
            "id", "doi", "title", "abstract", "references"
        )
    )
    checkable = [p for p in targets if normalize_doi(p.doi)]
    if not checkable:
        return {"checked": 0, "filled": 0, "batches": 0}
    own_client = client is None
    http = client or httpx.Client(headers=_headers(), follow_redirects=True)
    try:
        # Count batches the same way enrich_papers does.
        todo = [
            p
            for p in checkable
            if not (p.title or "").strip()
            or not abstract_available(p.abstract)
            or not has_reference_list(p)
        ]
        batches = (len(todo) + batch_size - 1) // batch_size if todo else 0
        filled = enrich_papers(todo, batch_size=batch_size, client=http)
        return {"checked": len(checkable), "filled": filled, "batches": batches}
    finally:
        if own_client:
            http.close()
