#!/usr/bin/env python3

'''
@File:   openalex.py

@Brief:  OpenAlex provider with polite pool and backoff (no scraping).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

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
    """Value object for one provider search page run."""

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
    paper_data: dict
    raw_response: dict


class OpenAlexError(RuntimeError):
    """Raised when OpenAlex cannot be queried after retries."""


class OpenAlexSource:
    """Concrete OpenAlex client for MVP (no abstract provider base yet)."""

    source_key = "openalex"
    base_url = "https://api.openalex.org/works"

    def __init__(self, email: str | None = None) -> None:
        configured = email or getattr(settings, "OPENALEX_EMAIL", "") or ""
        self.email = configured.strip()

    def default_headers(self) -> dict:
        agent = "LocalSurveyResearchFilter/0.1"
        if self.email:
            agent += f" (mailto:{self.email})"
        return {"User-Agent": agent, "Accept": "application/json"}

    def build_request_params(self, config: SearchConfig, cursor: str | None) -> dict:
        """Build OpenAlex filter/search/select params for one page."""
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
            raise OpenAlexError(f"OpenAlex unreachable after retries: {last_exc}")
        raise OpenAlexError(
            f"OpenAlex retryable {last_status} after {max_retries} tries"
        )

    def fetch(
        self,
        config: SearchConfig,
        client: httpx.Client | None = None,
    ) -> tuple[list[ProviderResult], int]:
        """Fetch up to max_pages. Returns (results, total_count)."""
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
                        ProviderResult(paper_data=paper_data, raw_response=item)
                    )
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            return results, total_count
        finally:
            if own_client:
                http.close()
