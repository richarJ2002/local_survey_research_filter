#!/usr/bin/env python3

"""
Fetch OpenAlex work pages with contact headers and retry backoff.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   openalex.py

@Brief:  Fetch OpenAlex work pages with contact headers and retry backoff.

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

from .mapping import map_openalex_to_canonical

# ------------------------------------------------------------------------ #
# Search config / Provider result
# ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class SearchConfig:
    """
    Hold immutable OpenAlex query, filters and pagination settings.

    query supplies search text; optional year_min/year_max bound dates. venues
    and author_names are OR-matched locally; keywords retains reusable rows
    but does not independently build this provider's query. Row tuples default
    to empty, year bounds to None, page_size to 25 and max_pages to 4.
    Values are not validated here; fetch caps page_size at 200 and attempts
    at least one page even when max_pages is below one.
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
    """
    Pair canonical paper_data with its original raw_response work object.

    Both dictionaries are in-memory metadata for the persistence caller;
    raw_response and paper_data['raw'] reference the same provider payload.
    """

    paper_data: dict
    raw_response: dict


class OpenAlexError(RuntimeError):
    """
    Report a rejected OpenAlex response or exhausted retry attempts.
    """


class OpenAlexSource:
    """
    Query OpenAlex works with an optional email in the User-Agent.

    email stores the stripped explicit or configured contact; the source does
    not own a persistent HTTP client or write papers to the database.
    """

    source_key = "openalex"
    base_url = "https://api.openalex.org/works"

    def __init__(self, email: str | None = None) -> None:
        """
        Set the contact email, falling back to settings for falsey input.

        Args:
            None

        Kwargs:
            email: Optional explicit contact email string. Defaults to None;
                None or an empty string uses OPENALEX_EMAIL from settings.

        Returns:
            None
        """
        configured = email or getattr(settings, "OPENALEX_EMAIL", "") or ""
        self.email = configured.strip()

    def default_headers(self) -> dict:
        """
        Build JSON request headers with the optional contact email.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: User-Agent identifies the app with optional mailto contact;
                Accept requests application/json. No API key is included.
        """
        agent = "LocalSurveyResearchFilter/0.1"
        if self.email:
            agent += f" (mailto:{self.email})"
        return {"User-Agent": agent, "Accept": "application/json"}

    def build_request_params(self, config: SearchConfig, cursor: str | None) -> dict:
        """
        Build OpenAlex search, date filters and field selection for a page.

        Args:
            config (SearchConfig): Query/year bounds and requested page size.
            cursor: Provider cursor string or None; falsey values use "*".

        Kwargs:
            None

        Returns:
            dict: search, per-page (capped at 200), cursor and select,
                plus filter for non-None inclusive year bounds. Venue/author
                text is filtered locally, not sent as provider IDs.
        """
        filters: list[str] = []
        if config.year_min is not None:
            filters.append(f"from_publication_date:{config.year_min}-01-01")
        if config.year_max is not None:
            filters.append(f"to_publication_date:{config.year_max}-12-31")
        params: dict = {
            "search": config.query,
            "per-page": min(config.page_size, 200),
            "cursor": cursor or "*",
            "select": (
                "id,doi,title,display_name,publication_year,publication_date,"
                "authorships,primary_location,locations,best_oa_location,"
                "abstract_inverted_index,cited_by_count,referenced_works,keywords"
            ),
        }
        if filters:
            params["filter"] = ",".join(filters)
        # Note: venue/author text filters are applied client-side in MVP
        # because OpenAlex needs IDs for precise filtering. We keep the raw
        # strings on Search for audit and filter titles/venues locally.
        return params

    def _get_with_backoff(
        self, client: httpx.Client, params: dict, max_retries: int = 3
    ) -> httpx.Response:
        """
        GET a works page, retrying selected transport errors and 429/5xx.

        Args:
            client (httpx.Client): Borrowed client; timeout is 30 seconds.
            params (dict): Query parameters for the fixed works endpoint.

        Kwargs:
            max_retries (int): Total attempt count, not extra retries.
                Defaults to 3; nonpositive values make no requests and fail.

        Returns:
            httpx.Response: First response other than 429/5xx, including
                nonretryable error statuses for fetch to check.

        Raises:
            OpenAlexError: If all attempts fail with handled transport errors
                or retryable statuses, or no attempt is allowed.
            httpx.RequestError: If a transport error other than ConnectError,
                ReadTimeout or RemoteProtocolError occurs.

        Side Effects:
            Issue GETs and sleep after handled failures. For HTTP retries use
            numeric Retry-After or exponential seconds plus jitter, capped at
            30 seconds; transport retries use exponential seconds plus jitter.
        """
        last_exc: Exception | None = None
        last_status: int | None = None
        for attempt in range(max_retries):
            try:
                resp = client.get(self.base_url, params=params, timeout=30.0)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
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
            raise OpenAlexError(
                f"OpenAlex unreachable after retries: {last_exc}")
        raise OpenAlexError(
            f"OpenAlex retryable {last_status} after {max_retries} tries"
        )

    def fetch(
        self,
        config: SearchConfig,
        client: httpx.Client | None = None,
    ) -> tuple[list[ProviderResult], int]:
        """
        Fetch cursor pages and apply local venue/author substring filters.

        Args:
            config (SearchConfig): Search text, year bounds, OR-matched local
                venue/author rows and pagination settings.

        Kwargs:
            client: Optional borrowed httpx.Client. Defaults
                to None, which creates and closes a redirect-following client
                with default_headers; borrowed clients keep their own headers.

        Returns:
            tuple: List of canonical/raw ProviderResult objects surviving
                local filters and integer meta.count before those filters or
                page truncation (zero if absent). Fetch at most
                max(1, config.max_pages) pages, stopping without a next_cursor;
                duplicate works are not removed here.

        Raises:
            OpenAlexError: If a request exhausts retries or returns non-200.
            httpx.RequestError: If an unhandled transport failure occurs.
            ValueError: If JSON, total count or canonical values cannot parse.
            TypeError: If provider fields have incompatible mapping types.

        Side Effects:
            Perform requests and retry sleeps, without DB writes. Close only
            a client created here; there is no fixed inter-page sleep.
        """
        own_client = client is None
        http = client or httpx.Client(
            headers=self.default_headers(), follow_redirects=True
        )
        try:
            results: list[ProviderResult] = []
            cursor: str | None = None
            total_count = 0
            for _ in range(max(1, config.max_pages)):
                params = self.build_request_params(config, cursor)
                resp = self._get_with_backoff(http, params)
                if resp.status_code != 200:
                    raise OpenAlexError(
                        f"OpenAlex {resp.status_code}: {resp.text[:500]}"
                    )
                payload = resp.json()
                if total_count == 0:
                    meta = payload.get("meta") or {}
                    total_count = int(meta.get("count") or 0)
                for item in payload.get("results", []) or []:
                    paper_data, _ = map_openalex_to_canonical(item)
                    # Lightweight client-side venue/author substring filter.
                    # Lists are OR-matched so one row hitting is enough.
                    if config.venues:
                        venue = (paper_data.get("venue") or "").lower()
                        if not any(v.lower() in venue for v in config.venues if v):
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
                        ProviderResult(paper_data=paper_data,
                                       raw_response=item)
                    )
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            return results, total_count
        finally:
            if own_client:
                http.close()
