#!/usr/bin/env python3

'''
@File:   semantic_scholar.py

@Brief:  Semantic Scholar provider with API-key polite use and backoff.

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

from .mapping import map_semantic_scholar_to_canonical

# ------------------------------------------------------------------------ #
# Search config / Provider result
# ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class SemanticScholarConfig:
    """Value object reusing sorted row lists for provider reuse."""

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


class SemanticScholarError(RuntimeError):
    """Raised when Semantic Scholar cannot be queried after retries."""


class SemanticScholarSource:
    """Concrete Semantic Scholar client (Graph API paper search)."""

    source_key = "semantic_scholar"
    base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    fields = (
        "title,abstract,year,publicationDate,venue,authors,"
        "externalIds,url,openAccessPdf,citationCount,fieldsOfStudy"
    )

    def __init__(self, api_key: str | None = None) -> None:
        """Use explicit key or settings key; works without key at shared limits."""
        configured = api_key if api_key is not None else getattr(
            settings, "SEMANTIC_SCHOLAR_KEY", ""
        )
        self.api_key = (configured or "").strip()

    # --- Headers ---
    def default_headers(self) -> dict:
        """Build headers with optional x-api-key (never logged)."""
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    # --- Year param ---
    def _year_param(self, config: SemanticScholarConfig) -> str | None:
        """Format year range as S2 expects, or None for max range."""
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
        """Build S2 search params for one offset page."""
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
        """GET with retry on 429/5xx plus polite 1s pacing when keyed."""
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
            raise SemanticScholarError(f"S2 unreachable after retries: {last_exc}")
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
        """Fetch up to max_pages via offset. Returns (results, total)."""
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
                        ProviderResult(paper_data=paper_data, raw_response=item)
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
