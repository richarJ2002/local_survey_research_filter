#!/usr/bin/env python3

'''
@File:   crossref.py

@Brief:  Crossref provider (DOI metadata, polite mailto, no key).

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

from .mapping import map_crossref_to_canonical

# ------------------------------------------------------------------------ #
# Config / result / errors
# ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class CrossrefConfig:
    """Value object reusing sorted row lists."""

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


class CrossrefError(RuntimeError):
    """Raised when Crossref cannot be queried after retries."""


class CrossrefSource:
    """Concrete Crossref client (api.crossref.org/works)."""

    source_key = "crossref"
    base_url = "https://api.crossref.org/works"

    def __init__(self, email: str | None = None) -> None:
        """Use polite mailto for 50 req/s pool."""
        configured = email if email is not None else getattr(
            settings, "OPENALEX_EMAIL", ""
        )
        self.email = (configured or "").strip()

    # --- Headers ---
    def default_headers(self) -> dict:
        """Build UA with optional mailto (never logs secrets)."""
        agent = "LocalSurveyResearchFilter/0.1"
        if self.email:
            agent += f" (mailto:{self.email})"
        return {"User-Agent": agent, "Accept": "application/json"}

    # --- Params ---
    def build_request_params(
        self, config: CrossrefConfig, offset: int
    ) -> dict:
        """Build Crossref query/filter/rows params for one page."""
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
            params["query.container-title"] = " ".join(v for v in config.venues if v)
        if config.author_names:
            params["query.author"] = " ".join(n for n in config.author_names if n)
        if self.email:
            params["mailto"] = self.email
        return params

    # --- Backoff GET ---
    def _get_with_backoff(
        self, client: httpx.Client, params: dict, max_retries: int = 3
    ) -> httpx.Response:
        """GET with retry on 429/5xx honoring Retry-After."""
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
        """Fetch offset pages. Returns (results, total)."""
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
                        ProviderResult(paper_data=paper_data, raw_response=item)
                    )
                offset += len(batch)
                if offset >= total:
                    break
            return results, total
        finally:
            if own_client:
                http.close()
