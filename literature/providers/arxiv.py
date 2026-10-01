#!/usr/bin/env python3

'''
@File:   arxiv.py

@Brief:  arXiv provider (preprints, Atom feed, no key, polite pacing).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

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
    """Value object reusing sorted row lists."""

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
    paper_data: dict
    raw_response: dict


class ArxivError(RuntimeError):
    """Raised when arXiv cannot be queried after retries."""


class ArxivSource:
    """Concrete arXiv client (export.arxiv.org/api/query, Atom)."""

    source_key = "arxiv"
    base_url = "https://export.arxiv.org/api/query"
    namespaces = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    # --- Headers ---
    def default_headers(self) -> dict:
        """Build UA (no secrets involved)."""
        return {"User-Agent": "LocalSurveyResearchFilter/0.1", "Accept": "application/atom+xml"}

    # --- Query string ---
    def build_search_query(self, config: ArxivConfig) -> str:
        """Build all:kw search from keyword rows (OR-joined)."""
        terms = [k for k in config.keywords if k] or ([config.query] if config.query else [])
        if not terms:
            return "all:*"
        quoted = [f'all:"{t}"' for t in terms]
        return " OR ".join(quoted)

    # --- Backoff GET ---
    def _get_with_backoff(
        self, client: httpx.Client, params: dict, max_retries: int = 3
    ) -> httpx.Response:
        """GET with retry; arXiv asks 3s pacing and 503 backoff."""
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
        """Parse Atom entries into raw dicts for canonical mapping."""
        # Guard: bound entity-expansion (billion laughs) without new deps.
        # ET does not fetch external entities; size cap bounds expansion.
        if len(text) > 2_000_000:
            raise ArxivError("arXiv feed too large (>2MB), refusing to parse")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ArxivError(f"arXiv invalid Atom: {exc}") from exc
        items: list[dict] = []
        for entry in root.findall("atom:entry", self.namespaces):
            title = (entry.findtext("atom:title", default="", namespaces=self.namespaces) or "").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=self.namespaces) or "").strip()
            published = entry.findtext("atom:published", default="", namespaces=self.namespaces) or ""
            entry_id = entry.findtext("atom:id", default="", namespaces=self.namespaces) or ""
            authors = [
                (a.findtext("atom:name", default="", namespaces=self.namespaces) or "").strip()
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
        """Fetch start-offset pages. Returns (results, total_guess)."""
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
                    raise ArxivError(f"arXiv {resp.status_code}: {resp.text[:500]}")
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
