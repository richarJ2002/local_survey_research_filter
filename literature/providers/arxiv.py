#!/usr/bin/env python3

"""
Fetch and map arXiv Atom search pages without an API key.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   arxiv.py

@Brief:  Fetch and map arXiv Atom search pages without an API key.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import random
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# Custom library Imports
import httpx

from .mapping import map_arxiv_to_canonical

# ------------------------------------------------------------------------ #
# Config / result / errors
# ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class ArxivConfig:
    """
    Hold immutable arXiv query rows, local filters and page settings.

    Nonempty keywords override query when building OR-joined all: terms.
    year_min/year_max default to None and filter dated results locally;
    author_names is locally OR-matched, while venues is retained but unused.
    Row tuples default to empty, page_size to 25 and max_pages to 2. No
    validation occurs here; fetch caps page_size at 50 and attempts at least
    one page even when max_pages is below one.
    """

    query: str
    year_min: int | None = None
    year_max: int | None = None
    keywords: tuple = ()
    venues: tuple = ()
    author_names: tuple = ()
    page_size: int = 25
    max_pages: int = 2


@dataclass(frozen=True)
class ProviderResult:
    """
    Pair canonical paper_data with the parsed raw_response Atom entry.

    raw_response and paper_data['raw'] reference the same entry dictionary;
    neither represents a database write or a full XML response.
    """

    paper_data: dict
    raw_response: dict


class ArxivError(RuntimeError):
    """
    Report failed arXiv requests or invalid/oversize XML.
    """


class ArxivSource:
    """
    Query the arXiv export Atom endpoint with retry and page pacing.

    namespaces supplies Atom/arXiv XML names. No API key or persistent client
    is held; callers persist the returned canonical/raw entry pairs.
    """

    source_key = "arxiv"
    base_url = "https://export.arxiv.org/api/query"
    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    # --- Headers ---
    def default_headers(self) -> dict:
        """
        Build key-free Atom request headers.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: User-Agent identifies the app and Accept requests
                application/atom+xml; no contact credentials are included.
        """
        return {"User-Agent": "LocalSurveyResearchFilter/0.1", "Accept": "application/atom+xml"}

    # --- Query string ---
    def build_search_query(self, config: ArxivConfig) -> str:
        """
        Build an OR-joined arXiv all-field query from reusable rows.

        Args:
            config (ArxivConfig): Keywords, falling back to query if no
                nonempty keyword is available.

        Kwargs:
            None

        Returns:
            str: Nonempty terms wrapped as all:"term" and OR-joined, or all:*
                when both rows and query are empty. Terms are not escaped here.
        """
        terms = [k for k in config.keywords if k] or (
            [config.query] if config.query else [])
        if not terms:
            return "all:*"
        quoted = [f'all:"{t}"' for t in terms]
        return " OR ".join(quoted)

    # --- Backoff GET ---
    def _get_with_backoff(
        self, client: httpx.Client, params: dict, max_retries: int = 3
    ) -> httpx.Response:
        """
        GET an Atom page with arXiv-specific retry sleeps.

        Args:
            client (httpx.Client): Borrowed client; timeout is 30 seconds.
            params (dict): Query parameters for the fixed export endpoint.

        Kwargs:
            max_retries (int): Total attempt count, not extra retries.
                Defaults to 3; nonpositive values make no requests and fail.

        Returns:
            httpx.Response: First response other than 429/5xx, including other
                error statuses for fetch to check.

        Raises:
            ArxivError: If handled failures exhaust all attempts or none is
                allowed.
            httpx.RequestError: If a transport error other than ConnectError,
                ReadTimeout or RemoteProtocolError occurs.

        Side Effects:
            Issue GETs; after transport failures sleep 3 + 2**attempt seconds,
            and after 429/5xx sleep min(30 + 2**attempt, 60) plus up to one
            second of jitter. Retry-After is not interpreted here.
        """
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = client.get(self.base_url, params=params, timeout=30.0)
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                time.sleep(3 + (2**attempt))
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                time.sleep(min(30 + (2**attempt), 60) + random.uniform(0, 1))
                continue
            return resp
        if last_exc is not None:
            raise ArxivError(f"arXiv unreachable: {last_exc}")
        raise ArxivError("arXiv retryable errors after retries")

    # --- Parse Atom ---
    def parse_feed(self, text: str) -> list[dict]:
        """
        Parse a bounded Atom text input into raw entry dictionaries.

        Args:
            text (str): Downloaded XML text, limited to 2,000,000 characters
                before parsing (not a streaming or byte-count limit).

        Kwargs:
            None

        Returns:
            list[dict]: Feed-order rows with id, whitespace-collapsed title,
                stripped summary, published text, stripped author names, doi,
                journal_ref, primary_category and category terms. Missing text
                becomes blank and missing lists empty; no entries yields [].

        Raises:
            ArxivError: If the text exceeds the size cap or XML parsing fails.
        """
        # Bound parser input; ElementTree does not fetch external entities.
        # The text cap is not a bound on the size of expanded entity content.
        if len(text) > 2_000_000:
            raise ArxivError("arXiv feed too large (>2MB), refusing to parse")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ArxivError(f"arXiv invalid Atom: {exc}") from exc
        items: list[dict] = []
        for entry in root.findall("atom:entry", self.namespaces):
            title = (entry.findtext("atom:title", default="",
                     namespaces=self.namespaces) or "").strip()
            summary = (entry.findtext("atom:summary", default="",
                       namespaces=self.namespaces) or "").strip()
            published = entry.findtext(
                "atom:published", default="", namespaces=self.namespaces) or ""
            entry_id = entry.findtext(
                "atom:id", default="", namespaces=self.namespaces) or ""
            authors = [
                (a.findtext("atom:name", default="",
                 namespaces=self.namespaces) or "").strip()
                for a in entry.findall("atom:author", self.namespaces)
            ]
            doi = ""
            journal_ref = ""
            primary_cat = ""
            cats: list[str] = []
            for child in entry:
                tag = child.tag
                if tag.endswith("}doi"):
                    doi = (child.text or "").strip()
                elif tag.endswith("}journal_ref"):
                    journal_ref = (child.text or "").strip()
                elif tag.endswith("}primary_category"):
                    primary_cat = (child.attrib.get("term") or "").strip()
                elif tag.endswith("}category") or tag == "category":
                    term = (child.attrib.get("term") or "").strip()
                    if term:
                        cats.append(term)
            items.append(
                {
                    "id": entry_id,
                    "title": " ".join(title.split()),
                    "summary": summary,
                    "published": published,
                    "authors": authors,
                    "doi": doi,
                    "journal_ref": journal_ref,
                    "primary_category": primary_cat,
                    "categories": cats,
                }
            )
        return items

    # --- Fetch ---
    def fetch(
        self, config: ArxivConfig, client: httpx.Client | None = None
    ) -> tuple[list[ProviderResult], int]:
        """
        Fetch arXiv offset pages and apply local year/author filters.

        Args:
            config (ArxivConfig): Query rows, year bounds, OR-matched author
                rows and page settings; venue rows do not filter this feed.

        Kwargs:
            client: Optional borrowed httpx.Client. Defaults
                to None, which creates and closes a redirect-following client
                with default_headers; borrowed clients keep their own headers.

        Returns:
            tuple: List of canonical/raw ProviderResult objects passing
                local filters and integer len(results), not the feed's total
                match count. Fetch at most max(1, config.max_pages) pages,
                stopping on empty/short batches. Undated papers survive year
                filtering; duplicate entries are not removed here.

        Raises:
            ArxivError: If retries exhaust, status is non-200, or XML fails
                size/parsing checks.
            httpx.RequestError: If an unhandled transport failure occurs.

        Side Effects:
            Request submitted-date-descending pages and sleep 3.2 seconds
            after each full batch, including a final page at the page ceiling.
            Retry sleeps may also occur; close only a client created here and
            do not write to the database.
        """
        own_client = client is None
        http = client or httpx.Client(
            headers=self.default_headers(), follow_redirects=True
        )
        try:
            results: list[ProviderResult] = []
            start = 0
            for _ in range(max(1, config.max_pages)):
                params = {
                    "search_query": self.build_search_query(config),
                    "start": start,
                    "max_results": min(config.page_size, 50),
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                }
                resp = self._get_with_backoff(http, params)
                if resp.status_code != 200:
                    raise ArxivError(
                        f"arXiv {resp.status_code}: {resp.text[:500]}")
                batch = self.parse_feed(resp.text)
                if not batch:
                    break
                for raw in batch:
                    paper_data, _ = map_arxiv_to_canonical(raw)
                    # Client-side year/author filtering (feed has no venue filter).
                    year = paper_data.get("publication_year")
                    if config.year_min and year and year < config.year_min:
                        continue
                    if config.year_max and year and year > config.year_max:
                        continue
                    if config.author_names:
                        hay = " ".join(
                            str(a.get("name", ""))
                            for a in paper_data.get("authors", [])
                            if isinstance(a, dict)
                        ).lower()
                        if not any(n.lower() in hay for n in config.author_names if n):
                            continue
                    results.append(
                        ProviderResult(paper_data=paper_data, raw_response=raw)
                    )
                start += len(batch)
                if len(batch) < min(config.page_size, 50):
                    break
                time.sleep(3.2)
            return results, len(results)
        finally:
            if own_client:
                http.close()
