#!/usr/bin/env python3

"""Fetch Semantic Scholar search pages with optional key headers and backoff.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   semantic_scholar.py

@Brief:  Fetch Semantic Scholar search pages with optional key headers and backoff.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import random
import time
from dataclasses import dataclass

# Custom library Imports
import httpx
from django.conf import settings

from .mapping import map_semantic_scholar_to_canonical

# ------------------------------------------------------------------------ #
# Search config / Provider result
# ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class SemanticScholarConfig:
    """Hold immutable Graph API query, reusable rows and page settings.

    query supplies search text; year_min/year_max default to None, and
    keywords/venues/author_names to empty tuples. keywords does not rebuild
    query here; venues is sent to the provider and authors are OR-matched
    locally. page_size defaults to 25 and max_pages to 4; no validation occurs
    here, and fetch caps page_size at 100 and attempts at least one page.
    """

    query: str
    year_min: int | None = None
    year_max: int | None = None
    keywords: tuple = ()
    venues: tuple = ()
    author_names: tuple = ()
    page_size: int = 25
    max_pages: int = 4


@dataclass(frozen=True)
class ProviderResult:
    """Pair canonical paper_data with its original raw_response paper object.

    raw_response and paper_data['raw'] reference the same Graph API payload;
    callers own subsequent persistence, not this result container.
    """

    paper_data: dict
    raw_response: dict


class SemanticScholarError(RuntimeError):
    """Report a rejected Semantic Scholar response or exhausted retries."""


class SemanticScholarSource:
    """Query Graph API paper search with an optional stripped api_key.

    Send the key only through x-api-key headers, never query parameters or
    logging. The source does not own a persistent client or write papers.
    """

    source_key = "semantic_scholar"
    base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    fields = (
        "title,abstract,year,publicationDate,venue,authors,"
        "externalIds,url,openAccessPdf,citationCount,fieldsOfStudy"
    )

    def __init__(self, api_key: str | None = None) -> None:
        """Set the explicit or configured API key for later request headers.

        Args:
            None

        Kwargs:
            api_key: Optional explicit key string. Defaults to None, reading
                SEMANTIC_SCHOLAR_KEY; an empty string selects anonymous access.

        Returns:
            None
        """
        configured = api_key if api_key is not None else getattr(
            settings, "SEMANTIC_SCHOLAR_KEY", ""
        )
        self.api_key = (configured or "").strip()

    # --- Headers ---
    def default_headers(self) -> dict:
        """Build JSON request headers with an optional x-api-key.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: Accept requests application/json; x-api-key is present only
                when api_key is nonblank. This helper does not log the key.
        """
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    # --- Year param ---
    def _year_param(self, config: SemanticScholarConfig) -> str | None:
        """Format inclusive year bounds for the Graph API.

        Args:
            config (SemanticScholarConfig): Optional year_min/year_max bounds.

        Kwargs:
            None

        Returns:
            str | None: "min-max", "min-" or "-max" for truthy bounds; None
                when both bounds are falsey, meaning no requested year filter.
        """
        if config.year_min and config.year_max:
            return f"{config.year_min}-{config.year_max}"
        if config.year_min:
            return f"{config.year_min}-"
        if config.year_max:
            return f"-{config.year_max}"
        return None

    # --- Request params ---
    def build_request_params(
        self, config: SemanticScholarConfig, offset: int
    ) -> dict:
        """Build Graph API search parameters for an offset page.

        Args:
            config (SemanticScholarConfig): Query, year/venue rows, page size.
            offset (int): Number of provider results to skip before this page.

        Kwargs:
            None

        Returns:
            dict: query, fields, limit (capped at 100) and offset, plus year
                when bounded and comma-joined venue rows when configured.
                Author text filtering occurs locally, not in these parameters.
        """
        params: dict = {
            "query": config.query,
            "fields": self.fields,
            "limit": min(config.page_size, 100),
            "offset": offset,
        }
        year = self._year_param(config)
        if year:
            params["year"] = year
        if config.venues:
            params["venue"] = ",".join(v for v in config.venues if v)
        return params

    # --- Backoff GET ---
    def _get_with_backoff(
        self, client: httpx.Client, params: dict, max_retries: int = 3
    ) -> httpx.Response:
        """GET a search page with retry backoff for selected failures.

        Args:
            client (httpx.Client): Borrowed client; timeout is 30 seconds.
            params (dict): Parameters for the fixed paper-search endpoint.

        Kwargs:
            max_retries (int): Total attempt count, not extra retries.
                Defaults to 3; nonpositive values make no requests and fail.

        Returns:
            httpx.Response: First response other than 429/5xx, including other
                error statuses for fetch to check.

        Raises:
            SemanticScholarError: If handled failures exhaust attempts or none
                is allowed.
            httpx.RequestError: If a transport error other than ConnectError,
                ReadTimeout or RemoteProtocolError occurs.

        Side Effects:
            Issue GETs and sleep after handled failures; HTTP retries use
            numeric Retry-After or exponential seconds plus jitter, capped at
            30 seconds. Transport retries use exponential seconds plus jitter.
            Successful requests have no pacing sleep in this helper.
        """
        last_exc: Exception | None = None
        last_status: int | None = None
        for attempt in range(max_retries):
            try:
                resp = client.get(self.base_url, params=params, timeout=30.0)
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                time.sleep((2**attempt) + random.uniform(0, 0.5))
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_status = resp.status_code
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else (2**attempt)
                except ValueError:
                    wait = 2**attempt
                time.sleep(min(wait + random.uniform(0, 0.5), 30.0))
                continue
            return resp
        if last_exc is not None:
            raise SemanticScholarError(
                f"S2 unreachable after retries: {last_exc}")
        raise SemanticScholarError(
            f"S2 retryable {last_status} after {max_retries} tries "
            "(shared limits or invalid key — check Settings, then retry)"
        )

    # --- Fetch pages ---
    def fetch(
        self,
        config: SemanticScholarConfig,
        client: httpx.Client | None = None,
    ) -> tuple[list[ProviderResult], int]:
        """Fetch offset pages and apply local author substring filters.

        Args:
            config (SemanticScholarConfig): Query, provider year/venue filters,
                local OR-matched author rows and pagination settings.

        Kwargs:
            client: Optional borrowed httpx.Client. Defaults
                to None, which creates and closes a redirect-following client
                with default_headers; borrowed clients keep their own headers.

        Returns:
            tuple: List of canonical/raw ProviderResult objects surviving
                local author filtering and the first page's integer total
                before filtering/truncation (zero if absent). Fetch at most
                max(1, config.max_pages) pages; stop on an empty page or offset
                reaching total. No result deduplication occurs here.

        Raises:
            SemanticScholarError: If retries exhaust or a status is non-200.
            httpx.RequestError: If an unhandled transport failure occurs.
            ValueError: If JSON, total count or canonical values cannot parse.
            TypeError: If provider fields have incompatible mapping types.

        Side Effects:
            Make requests and retry sleeps without DB writes. Sleep 1.1 seconds
            after each nonempty page below total, including the final page at
            the page ceiling, whether keyed or anonymous. Close only a client
            created here.
        """
        own_client = client is None
        http = client or httpx.Client(
            headers=self.default_headers(), follow_redirects=True
        )
        try:
            results: list[ProviderResult] = []
            total = 0
            offset = 0
            for page in range(max(1, config.max_pages)):
                params = self.build_request_params(config, offset)
                resp = self._get_with_backoff(http, params)
                if resp.status_code != 200:
                    raise SemanticScholarError(
                        f"S2 {resp.status_code}: {resp.text[:500]}"
                    )
                payload = resp.json()
                if page == 0:
                    total = int(payload.get("total") or 0)
                batch = payload.get("data") or []
                if not batch:
                    break
                for item in batch:
                    paper_data, _ = map_semantic_scholar_to_canonical(item)
                    if config.author_names:
                        hay = " ".join(
                            str(a.get("name", ""))
                            for a in paper_data.get("authors", [])
                            if isinstance(a, dict)
                        ).lower()
                        if not any(
                            n.lower() in hay for n in config.author_names if n
                        ):
                            continue
                    results.append(
                        ProviderResult(paper_data=paper_data,
                                       raw_response=item)
                    )
                offset += len(batch)
                if offset >= total:
                    break
                # Polite pacing: ~1 req/s authenticated, gentler anonymous.
                time.sleep(1.1)
            return results, total
        finally:
            if own_client:
                http.close()
