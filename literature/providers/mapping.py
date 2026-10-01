#!/usr/bin/env python3

'''
@File:   mapping.py

@Brief:  Pure OpenAlex -> canonical Paper mapping (no Django deps).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import string
from datetime import date, datetime

# Custom library Imports
# None

# ------------------------------------------------------------------------ #
# Abstract / Date / URL helpers
# ------------------------------------------------------------------------ #


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """Rebuild abstract text from OpenAlex inverted index.

    Format: {"word": [positions...]}. Returns "" when missing.
    """
    if not inverted_index:
        return ""
    try:
        max_pos = max(pos for positions in inverted_index.values() for pos in positions)
    except ValueError:
        return ""
    words: list[str] = [""] * (max_pos + 1)
    for word, positions in inverted_index.items():
        for pos in positions:
            if 0 <= pos <= max_pos:
                words[pos] = word
    return " ".join(w for w in words if w)


def parse_date(date_str: str | None) -> date | None:
    """Parse YYYY-MM-DD, returning None on missing/invalid input."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def extract_urls(item: dict) -> list[dict]:
    """Collect pdf/landing urls without duplicates."""
    urls: list[dict] = []
    seen: set[str] = set()

    def _add(url: str | None, label: str) -> None:
        if url and url not in seen:
            seen.add(url)
            urls.append({"url": url, "label": label})

    primary = item.get("primary_location") or {}
    _add(primary.get("pdf_url"), "pdf")
    _add(primary.get("landing_page_url"), "landing")
    best_oa = item.get("best_oa_location") or {}
    _add(best_oa.get("pdf_url"), "pdf")
    for loc in item.get("locations") or []:
        if isinstance(loc, dict):
            _add(loc.get("pdf_url"), "pdf")
    return urls


def map_openalex_to_canonical(item: dict) -> tuple[dict, dict]:
    """Map one OpenAlex work to (paper_data, discovery_data) dicts."""
    raw_id = str(item.get("id", ""))
    openalex_id = raw_id.replace("https://openalex.org/", "") or None

    raw_doi = (item.get("doi") or "").strip()
    doi = raw_doi.replace("https://doi.org/", "").lower() or None

    authors: list[dict] = []
    for auth in item.get("authorships", []) or []:
        author = (auth or {}).get("author") or {}
        institutions = (auth or {}).get("institutions") or []
        affiliations = [
            str(inst.get("display_name", ""))
            for inst in institutions
            if isinstance(inst, dict) and inst.get("display_name")
        ]
        authors.append(
            {
                "name": str(author.get("display_name", "")),
                "orcid": str(author.get("orcid") or "").replace(
                    "https://orcid.org/", ""
                ),
                "affiliations": affiliations,
            }
        )

    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    keywords_raw = item.get("keywords") or []
    keywords = [
        str(kw.get("display_name", ""))
        for kw in keywords_raw
        if isinstance(kw, dict) and kw.get("display_name")
    ]

    paper_data = {
        "openalex_id": openalex_id,
        "title": str(item.get("title") or item.get("display_name") or ""),
        "authors": authors,
        "abstract": reconstruct_abstract(item.get("abstract_inverted_index")),
        "publication_year": item.get("publication_year"),
        "publication_date": parse_date(item.get("publication_date")),
        "venue": str(source.get("display_name") or ""),
        "publisher": str(source.get("publisher") or ""),
        "doi": doi,
        "urls": extract_urls(item),
        "keywords": keywords,
        "citation_count": int(item.get("cited_by_count") or 0),
        "references": list(item.get("referenced_works") or []),
        "source_ids": {"openalex": openalex_id} if openalex_id else {},
        "sources": ["openalex"],
        "raw": item,
    }
    discovery_data = {"source": "openalex"}
    return paper_data, discovery_data


# ------------------------------------------------------------------------ #
# Crossref mapping
# ------------------------------------------------------------------------ #

# --- Map one Crossref work ---
def map_crossref_to_canonical(item: dict) -> tuple[dict, dict]:
    """Map one Crossref /works item to canonical dicts (often no abstract)."""
    raw_doi = str(item.get("DOI") or "").strip()
    doi = raw_doi.lower() or None

    authors: list[dict] = []
    for entry in item.get("author") or []:
        if not isinstance(entry, dict):
            continue
        given = str(entry.get("given") or "").strip()
        family = str(entry.get("family") or "").strip()
        name = f"{given} {family}".strip() or str(entry.get("name") or "")
        authors.append({"name": name, "orcid": str(entry.get("ORCID") or "").replace("https://orcid.org/", ""), "affiliations": []})

    titles = item.get("title") or []
    title = str(titles[0]) if titles else ""
    venues = item.get("container-title") or []
    venue = str(venues[0]) if venues else ""
    subjects = item.get("subject") or []
    keywords = [str(s) for s in subjects if s]

    year: int | None = None
    pub_date = None
    for key in ("published-print", "published-online", "published", "created"):
        node = item.get(key) or {}
        parts = node.get("date-parts") if isinstance(node, dict) else None
        if parts and parts[0]:
            try:
                year = int(parts[0][0])
                if len(parts[0]) >= 3:
                    pub_date = parse_date(f"{parts[0][0]:04d}-{parts[0][1]:02d}-{parts[0][2]:02d}")
                break
            except (ValueError, TypeError, IndexError):
                continue

    urls: list[dict] = []
    if item.get("URL"):
        urls.append({"url": str(item["URL"]), "label": "landing"})

    paper_data = {
        "openalex_id": None,
        "title": title,
        "authors": authors,
        "abstract": "",
        "publication_year": year,
        "publication_date": pub_date,
        "venue": venue,
        "publisher": str(item.get("publisher") or ""),
        "doi": doi,
        "urls": urls,
        "keywords": keywords,
        "citation_count": int(item.get("is-referenced-by-count") or 0),
        "references": [],
        "source_ids": {"crossref": doi} if doi else {},
        "sources": ["crossref"],
        "raw": item,
    }
    return paper_data, {"source": "crossref"}


# ------------------------------------------------------------------------ #
# arXiv mapping
# ------------------------------------------------------------------------ #

# --- Map one arXiv entry ---
def map_arxiv_to_canonical(item: dict) -> tuple[dict, dict]:
    """Map one parsed arXiv Atom entry to canonical dicts."""
    entry_id = str(item.get("id") or "")
    arxiv_id = entry_id.rsplit("/abs/", 1)[-1] if "/abs/" in entry_id else entry_id
    raw_doi = str(item.get("doi") or "").strip()
    doi = raw_doi.replace("https://doi.org/", "").lower() or None

    authors = [{"name": str(n), "orcid": "", "affiliations": []} for n in item.get("authors") or [] if str(n).strip()]
    cats = [str(c) for c in item.get("categories") or [] if c]
    if item.get("primary_category") and item["primary_category"] not in cats:
        cats.insert(0, str(item["primary_category"]))

    urls: list[dict] = []
    if entry_id:
        urls.append({"url": entry_id, "label": "landing"})

    pub_raw = str(item.get("published") or "")
    year: int | None = None
    pub_date = None
    if pub_raw:
        try:
            pub_date = parse_date(pub_raw[:10])
            year = int(pub_raw[:4])
        except ValueError:
            year = None

    paper_data = {
        "openalex_id": None,
        "title": str(item.get("title") or ""),
        "authors": authors,
        "abstract": str(item.get("summary") or ""),
        "publication_year": year,
        "publication_date": pub_date,
        "venue": str(item.get("journal_ref") or ""),
        "publisher": "",
        "doi": doi,
        "urls": urls,
        "keywords": cats,
        "citation_count": 0,
        "references": [],
        "source_ids": {"arxiv": arxiv_id} if arxiv_id else {},
        "sources": ["arxiv"],
        "raw": item,
    }
    return paper_data, {"source": "arxiv"}


# ------------------------------------------------------------------------ #
# Semantic Scholar mapping
# ------------------------------------------------------------------------ #

# --- Map one S2 paper ---
def map_semantic_scholar_to_canonical(item: dict) -> tuple[dict, dict]:
    """Map one Semantic Scholar paper/search item to canonical dicts."""
    paper_id = str(item.get("paperId") or "")
    external = item.get("externalIds") or {}
    raw_doi = str(external.get("DOI") or item.get("doi") or "").strip()
    doi = raw_doi.replace("https://doi.org/", "").lower() or None

    authors: list[dict] = []
    for entry in item.get("authors") or []:
        if not isinstance(entry, dict):
            continue
        authors.append(
            {
                "name": str(entry.get("name") or ""),
                "orcid": "",
                "affiliations": [],
            }
        )

    urls: list[dict] = []
    seen: set[str] = set()

    def _add(url: str | None, label: str) -> None:
        if url and url not in seen:
            seen.add(url)
            urls.append({"url": url, "label": label})

    _add((item.get("openAccessPdf") or {}).get("url"), "pdf")
    _add(item.get("url"), "landing")

    fields = item.get("fieldsOfStudy")
    keywords = [str(k) for k in fields if k] if isinstance(fields, list) else []

    paper_data = {
        "openalex_id": None,
        "title": str(item.get("title") or ""),
        "authors": authors,
        "abstract": str(item.get("abstract") or ""),
        "publication_year": item.get("year"),
        "publication_date": parse_date(item.get("publicationDate")),
        "venue": str(item.get("venue") or ""),
        "publisher": "",
        "doi": doi,
        "urls": urls,
        "keywords": keywords,
        "citation_count": int(item.get("citationCount") or 0),
        "references": [],
        "source_ids": {"semantic_scholar": paper_id} if paper_id else {},
        "sources": ["semantic_scholar"],
        "raw": item,
    }
    return paper_data, {"source": "semantic_scholar"}


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation/whitespace for dedup comparison."""
    lowered = (title or "").lower().strip()
    table = str.maketrans("", "", string.punctuation)
    collapsed = " ".join(lowered.translate(table).split())
    return collapsed


def first_author_surname(authors: list) -> str:
    """Return lowercase surname of first author, or '' when unavailable."""
    if not authors:
        return ""
    first = authors[0]
    name = first.get("name", "") if isinstance(first, dict) else str(first)
    parts = str(name).strip().split()
    if not parts:
        return ""
    return parts[-1].lower().strip(string.punctuation)
