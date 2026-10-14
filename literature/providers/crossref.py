#!/usr/bin/env python3

"""Fetch Crossref DOI metadata pages with optional mailto contact and backoff.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   crossref.py

@Brief:  Fetch Crossref DOI metadata pages with optional mailto contact and backoff.

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

from .mapping import map_crossref_to_canonical

# ------------------------------------------------------------------------ #
# Config / result / errors
# ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class CrossrefConfig:
    """Hold immutable Crossref query, bibliographic filters and page settings.

    query is search text; year_min/year_max default to None, and keywords,
    venues and author_names to empty reusable tuples. keywords does not build
    the query here; venue/author rows become provider query parameters.
    page_size defaults to 25 and max_pages to 4. No validation occurs here;
    fetch caps page_size at 100 and attempts at least one page.
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
    """Pair canonical paper_data with its original raw_response work object.

    raw_response and paper_data['raw'] reference the same Crossref payload;
    the persistence caller owns subsequent database writes.
    """

    paper_data: dict
    raw_response: dict


class CrossrefError(RuntimeError):
    """Report a rejected Crossref response or exhausted retry attempts."""


class CrossrefSource:
    """Query Crossref works using the stripped optional email contact.

    email is sent through User-Agent and mailto parameters. The source holds
    no persistent HTTP client and does not save returned papers.
    """

    source_key = "crossref"
    base_url = "https://api.crossref.org/works"

    def __init__(self, email: str | None = None) -> None:
        """Set an explicit contact email or use the configured OpenAlex email.

        Args:
            None

        Kwargs:
            email: Optional contact email string. Defaults to None, which reads
                OPENALEX_EMAIL; an explicit empty string disables the contact.

        Returns:
            None
        """
        configured = email if email is not None else getattr(
            settings, "OPENALEX_EMAIL", ""
        )
        self.email = (configured or "").strip()

    # --- Headers ---
    def default_headers(self) -> dict:
        """Build JSON request headers with optional mailto contact.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict: User-Agent identifies the app with optional mailto contact;
                Accept requests application/json. No secret key is included.
        """
        agent = "LocalSurveyResearchFilter/0.1"
        if self.email:
            agent += f" (mailto:{self.email})"
        return {"User-Agent": agent, "Accept": "application/json"}

    # --- Params ---
    def build_request_params(
        self, config: CrossrefConfig, offset: int
    ) -> dict:
        """Build Crossref query, filter and field parameters for one page.

        Args:
            config (CrossrefConfig): Search text, year/venue/author filters and
                requested page size.
            offset (int): Number of provider results to skip before this page.

        Kwargs:
            None

        Returns:
            dict: query, rows (capped at 100), offset and select, with optional
                inclusive publication-date filter, space-joined venue/author
                query terms and mailto contact. Abstracts/references are not
                included in the selected search fields.
        """
        params: dict = {
            "query": config.query,
            "rows": min(config.page_size, 100),
            "offset": offset,
            "select": "DOI,title,author,published-print,published-online,created,"
            "container-title,publisher,URL,type,is-referenced-by-count,subject",
        }
        filters: list[str] = []
        if config.year_min:
            filters.append(f"from-pub-date:{config.year_min}-01-01")
        if config.year_max:
            filters.append(f"until-pub-date:{config.year_max}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        if config.venues:
            params["query.container-title"] = " ".join(
                v for v in config.venues if v)
        if config.author_names:
            params["query.author"] = " ".join(
                n for n in config.author_names if n)
        if self.email:
            params["mailto"] = self.email
        return params

    # --- Backoff GET ---
    def _get_with_backoff(
        self, client: httpx.Client, params: dict, max_retries: int = 3
    ) -> httpx.Response:
        """GET a works page with transport and retryable-status backoff.

        Args:
            client (httpx.Client): Borrowed client; timeout is 30 seconds.
            params (dict): Query parameters for the fixed works endpoint.

        Kwargs:
            max_retries (int): Total attempt count, not extra retries.
                Defaults to 3; nonpositive values make no requests and fail.

        Returns:
            httpx.Response: First response other than 429/5xx, including other
                error statuses for fetch to check.

        Raises:
            CrossrefError: If handled failures exhaust the attempts or no
                attempt is allowed.
            httpx.RequestError: If an error other than ConnectError,
                ReadTimeout or RemoteProtocolError occurs in transport.

        Side Effects:
            Issue GETs and sleep after failures; use numeric Retry-After
            or exponential seconds plus jitter for HTTP retries, capped at 30
            seconds. Transport retries use exponential seconds plus jitter.
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
                time.sleep((2**attempt) + random.uniform(0, 0.5))
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else (2**attempt)
                except ValueError:
                    wait = 2**attempt
                time.sleep(min(wait + random.uniform(0, 0.5), 30.0))
                continue
            return resp
        if last_exc is not None:
            raise CrossrefError(f"Crossref unreachable: {last_exc}")
        raise CrossrefError("Crossref retryable errors after retries")

    # --- Fetch ---
    def fetch(
        self, config: CrossrefConfig, client: httpx.Client | None = None
    ) -> tuple[list[ProviderResult], int]:
        """Fetch Crossref offset pages and map each returned work.

        Args:
            config (CrossrefConfig): Query, filters and pagination settings.

        Kwargs:
            client: Optional borrowed httpx.Client. Defaults
                to None, which creates and closes a redirect-following client
                with default_headers; borrowed clients keep their own headers.

        Returns:
            tuple: List of canonical/raw ProviderResult objects and the
                first page's integer total-results count, before truncation
                (zero if absent). Fetch max(1, config.max_pages) pages at most;
                stop on an empty page or reaching the reported total. Do not
                deduplicate works or perform additional local filtering.

        Raises:
            CrossrefError: If a request exhausts retries or returns non-200.
            httpx.RequestError: If an unhandled transport failure occurs.
            ValueError: If JSON, total count or canonical values cannot parse.
            TypeError: If provider fields have incompatible mapping types.

        Side Effects:
            Perform requests and retry sleeps without DB writes. Close only
            a client created here; no fixed inter-page sleep is enforced.
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
                    raise CrossrefError(
                        f"Crossref {resp.status_code}: {resp.text[:500]}"
                    )
                message = (resp.json().get("message") or {})
                if page == 0:
                    total = int(message.get("total-results") or 0)
                batch = message.get("items") or []
                if not batch:
                    break
                for item in batch:
                    paper_data, _ = map_crossref_to_canonical(item)
                    results.append(
                        ProviderResult(paper_data=paper_data,
                                       raw_response=item)
                    )
                offset += len(batch)
                if offset >= total:
                    break
            return results, total
        finally:
            if own_client:
                http.close()
