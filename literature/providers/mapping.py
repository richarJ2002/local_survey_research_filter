#!/usr/bin/env python3

"""Map scholarly provider payloads to canonical paper/discovery dictionaries.

Keep mapping pure, without Django or provider requests.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   mapping.py

@Brief:  Map scholarly provider payloads to canonical paper/discovery dictionaries.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

import string
from datetime import date, datetime
from urllib.parse import urlsplit

# Custom library Imports
# None

# ------------------------------------------------------------------------ #
# Abstract / Date / URL helpers
# ------------------------------------------------------------------------ #


def reconstruct_abstract(inverted_index: dict | None) -> str:
    """Rebuild abstract text from OpenAlex inverted index.

    Args:
        inverted_index: OpenAlex {word: [zero-based positions]} dictionary
            with integer positions and string words, or None when absent.

    Kwargs:
        None

    Returns:
        str: Space-joined words in position order, omitting empty gaps; empty
            when the mapping is missing or contains no positions. Later words
            assigned to a repeated position replace earlier ones.

    Raises:
        TypeError: If positions or words have incompatible types.
    """
    if not inverted_index:
        return ""
    try:
        max_pos = max(pos for positions in inverted_index.values()
                      for pos in positions)
    except ValueError:
        return ""
    words: list[str] = [""] * (max_pos + 1)
    for word, positions in inverted_index.items():
        for pos in positions:
            if 0 <= pos <= max_pos:
                words[pos] = word
    return " ".join(w for w in words if w)


def parse_date(date_str: str | None) -> date | None:
    """Parse provider date text as a calendar date.

    Args:
        date_str: Date string in strptime's %Y-%m-%d format, or None.

    Kwargs:
        None

    Returns:
        date | None: Parsed date with no time/timezone, or None for falsey text
            or a ValueError from invalid date syntax/calendar values.

    Raises:
        TypeError: If a truthy input is not a string.
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def extract_urls(item: dict) -> list[dict]:
    """Collect OpenAlex PDF and landing URLs without duplicates.

    Args:
        item (dict): Work payload with primary_location, best_oa_location and
            optional locations metadata.

    Kwargs:
        None

    Returns:
        list[dict]: Encounter-order {"url": value, "label": "pdf" or "landing"}
            rows from primary PDF/landing, best-open-access PDF, then location
            PDFs. Skip falsey URLs and keep the first label for duplicate URLs.
    """
    urls: list[dict] = []
    seen: set[str] = set()

    def _add(url: str | None, label: str) -> None:
        """Append a previously unseen, nonempty URL to the enclosing list.

        Args:
            url: URL string or None; falsey values are ignored.
            label (str): Label retained with the first occurrence of this URL.

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Update the enclosing seen set and urls list without I/O.
        """
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
    """Map one OpenAlex work to canonical paper and discovery metadata.

    Args:
        item (dict): OpenAlex work with provider-shaped nested metadata.

    Kwargs:
        None

    Returns:
        tuple: (paper_data, discovery_data) dictionaries. paper_data contains
            openalex_id (short ID), doi (lowercased without HTTPS resolver),
            title, reconstructed abstract, authors (name/orcid/affiliations),
            publication_year, parsed publication_date, venue/publisher from
            primary source, deduplicated labeled urls, keyword display names,
            integer citation_count, referenced_works as references, source_ids
            keyed by "openalex", sources=["openalex"], and raw pointing to
            item without copying it.
            Missing text/lists become blank/empty, IDs/date may be None, and
            citations default to zero. discovery_data has source="openalex".

    Raises:
        TypeError: If provider fields have incompatible mapping/parsing types.
        ValueError: If citation_count cannot be converted to an integer.
    """
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
    """Map one Crossref work to canonical paper and discovery metadata.

    Args:
        item (dict): Crossref /works item with DOI, titles, authors and dates.

    Kwargs:
        None

    Returns:
        tuple: paper_data and {"source": "crossref"} dictionaries. paper_data
            has openalex_id=None, lowercased doi, first title/container-title
            as title/venue, authors (name/orcid/empty affiliations), blank
            abstract, publication_year/date from the first usable print/online/
            published/created date, publisher, landing urls, subject keywords,
            integer citation_count, empty references, source_ids keyed by
            "crossref" DOI, sources=["crossref"], and raw referencing item.
            Missing text/lists become blank/empty, year/date/DOI may be None,
            and citations default to zero; this mapper never fills abstracts
            or references even if present in the provider payload.

    Raises:
        TypeError: If provider fields have incompatible mapping/parsing types.
        ValueError: If citation_count cannot be converted to an integer.
    """
    raw_doi = str(item.get("DOI") or "").strip()
    doi = raw_doi.lower() or None

    authors: list[dict] = []
    for entry in item.get("author") or []:
        if not isinstance(entry, dict):
            continue
        given = str(entry.get("given") or "").strip()
        family = str(entry.get("family") or "").strip()
        name = f"{given} {family}".strip() or str(entry.get("name") or "")
        authors.append({"name": name, "orcid": str(entry.get("ORCID") or "").replace(
            "https://orcid.org/", ""), "affiliations": []})

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
                    pub_date = parse_date(
                        f"{parts[0][0]:04d}-{parts[0][1]:02d}-{parts[0][2]:02d}")
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
    """Map one parsed arXiv Atom entry to canonical metadata.

    Args:
        item (dict): Parsed feed entry with id, title, summary, published,
            author names, optional doi/journal_ref and categories.

    Kwargs:
        None

    Returns:
        tuple: paper_data and {"source": "arxiv"} dictionaries. paper_data has
            openalex_id=None, title, authors with name/blank orcid/empty
            affiliations, summary as abstract, publication_year/date from
            published text,
            journal_ref as venue, blank publisher, normalized optional doi,
            entry landing urls, categories as keywords with primary category
            prepended if absent, zero citation_count, empty references,
            source_ids keyed by "arxiv" (version suffix retained),
            sources=["arxiv"], and raw referencing item. Missing text/lists
            become blank/empty and year/date/DOI may be None.
    """
    entry_id = str(item.get("id") or "")
    arxiv_id = entry_id.rsplit(
        "/abs/", 1)[-1] if "/abs/" in entry_id else entry_id
    raw_doi = str(item.get("doi") or "").strip()
    doi = raw_doi.replace("https://doi.org/", "").lower() or None

    authors = [{"name": str(n), "orcid": "", "affiliations": []}
               for n in item.get("authors") or [] if str(n).strip()]
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
    """Map one Semantic Scholar paper/search item to canonical metadata.

    Args:
        item (dict): Graph API paper object with provider-shaped nested fields.

    Kwargs:
        None

    Returns:
        tuple: paper_data and {"source": "semantic_scholar"} dictionaries.
            paper_data has openalex_id=None, title, abstract, authors
            (name/blank orcid/empty affiliations), year as publication_year,
            parsed publication_date, venue, blank publisher, normalized DOI
            from externalIds or doi, unique PDF/landing urls, fieldsOfStudy
            as keywords, integer citation_count, empty references, source_ids
            keyed by "semantic_scholar" paperId, sources=["semantic_scholar"],
            and raw referencing item. Missing text/lists become blank/empty,
            date/DOI may be None, and citations default to zero. Reference
            extraction is handled separately by DOI lookup, not this mapper.

    Raises:
        TypeError: If provider fields have incompatible mapping/parsing types.
        ValueError: If citation_count cannot be converted to an integer.
    """
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
        """Append a previously unseen, nonempty URL to the enclosing list.

        Args:
            url: URL string or None; falsey values are ignored.
            label (str): Label retained with the first occurrence of this URL.

        Kwargs:
            None

        Returns:
            None

        Side Effects:
            Update the enclosing seen set and urls list without I/O.
        """
        if url and url not in seen:
            seen.add(url)
            urls.append({"url": url, "label": label})

    _add((item.get("openAccessPdf") or {}).get("url"), "pdf")
    _add(item.get("url"), "landing")

    fields = item.get("fieldsOfStudy")
    keywords = [str(k)
                for k in fields if k] if isinstance(fields, list) else []

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


# --- Scopus STANDARD search metadata; no abstract/reference retrieval ---
def normalize_scopus_id(identifier: str | None) -> str | None:
    """Resolve Scopus EID, prefixed ID and bare ID to one numeric identity.

    Args:
        identifier: EID, SCOPUS_ID-prefixed value, bare digits, or None.

    Kwargs:
        None

    Returns:
        str | None: Positive ASCII-digit identifier with its spelling retained,
        or None for unknown, non-text or invalid identities. No I/O occurs.
    """
    if not isinstance(identifier, str):
        return None
    cleaned = identifier.strip().removeprefix("2-s2.0-").removeprefix(
        "SCOPUS_ID:")
    if cleaned.isascii() and cleaned.isdigit() and cleaned.lstrip("0"):
        return cleaned
    return None


def map_scopus_to_canonical(item: dict) -> tuple[dict, dict]:
    """Map a Scopus search entry without inventing abstracts or citation edges.

    Args:
        item: JSON search entry containing Scopus EID/ID, title, DOI,
            publication metadata, first author and optional public links.

    Kwargs:
        None

    Returns:
        tuple[dict, dict]: Canonical paper metadata and Scopus provenance.
        Missing values remain blank/None. STANDARD search supplies at most
        the first author here; abstracts, keywords and references remain
        empty for the existing completion workflow. Raw metadata retains
        the entry, not request headers, credentials or response headers.

    Raises:
        TypeError: If entries, text fields, links or count types are malformed.
        ValueError: If citation counts are not nonnegative integers.
    """
    if not isinstance(item, dict):
        raise TypeError("Invalid Scopus search entry.")

    def text(field: str) -> str:
        """Read an optional text field without coercing structured data.

        Args:
            field: Scopus entry field name.

        Kwargs:
            None

        Returns:
            str: Stripped text, or blank for a missing/null value.

        Raises:
            TypeError: If the field is not text or null.
        """
        value = item.get(field)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise TypeError("Invalid Scopus text field.")
        return value.strip()

    doi = text("prism:doi").lower().removeprefix(
        "https://doi.org/").removeprefix("http://doi.org/") or None
    identifier = text("eid") or text("dc:identifier").removeprefix(
        "SCOPUS_ID:")
    publication_date = parse_date(text("prism:coverDate"))
    author = text("dc:creator")
    citations = item.get("citedby-count")
    if citations is None or citations == "":
        citations = 0
    if isinstance(citations, bool) or not isinstance(citations, (int, str)):
        raise TypeError("Invalid Scopus citation count.")
    citation_count = int(citations)
    if citation_count < 0:
        raise ValueError("Invalid Scopus citation count.")
    links = item.get("link") or []
    if not isinstance(links, list):
        raise TypeError("Invalid Scopus links.")
    urls = []
    for link in links:
        if not isinstance(link, dict) or link.get("@ref") != "scopus" or not (
            isinstance(link.get("@href"), str)
        ):
            continue
        try:
            parsed = urlsplit(link["@href"])
            valid = parsed.port in (None, 80, 443)
        except ValueError:
            continue
        if (valid and parsed.scheme in ("http", "https")
                and parsed.hostname in ("www.scopus.com", "scopus.com")
                and not parsed.username and not parsed.password):
            urls.append({"url": parsed._replace(
                scheme="https", netloc=parsed.hostname).geturl(),
                "label": "landing"})
    scopus_id = normalize_scopus_id(identifier)
    if not urls and scopus_id:
        urls.append({"url": "https://www.scopus.com/record/display.uri?"
                     f"eid=2-s2.0-{scopus_id}", "label": "landing"})
    paper_data = {
        "openalex_id": None,
        "title": text("dc:title"),
        "authors": [{"name": author, "orcid": "", "affiliations": []}]
        if author else [],
        "abstract": "",
        "publication_year": publication_date.year if publication_date else None,
        "publication_date": publication_date,
        "venue": text("prism:publicationName"),
        "publisher": "",
        "doi": doi,
        "urls": urls,
        "keywords": [],
        "citation_count": citation_count,
        "references": [],
        "source_ids": {"scopus": identifier} if identifier else {},
        "sources": ["scopus"],
        "raw": item,
    }
    return paper_data, {"source": "scopus"}


def normalize_title(title: str) -> str:
    """Normalize a title for exact duplicate comparison.

    Args:
        title (str): Title text; falsey values are treated as blank.

    Kwargs:
        None

    Returns:
        str: Lowercased text with ASCII punctuation removed and whitespace
            collapsed to single spaces; empty for a falsey or stripped title.
    """
    lowered = (title or "").lower().strip()
    table = str.maketrans("", "", string.punctuation)
    collapsed = " ".join(lowered.translate(table).split())
    return collapsed


def first_author_surname(authors: list) -> str:
    """Extract a first author's last name token for comparison.

    Args:
        authors (list): Author dictionaries with name keys, or stringifiable
            name values; inspect only the first entry.

    Kwargs:
        None

    Returns:
        str: Lowercased final whitespace-separated name token with edge ASCII
            punctuation stripped, or empty when no first name token exists.
            This is a token heuristic, not a culturally aware surname parser.
    """
    if not authors:
        return ""
    first = authors[0]
    name = first.get("name", "") if isinstance(first, dict) else str(first)
    parts = str(name).strip().split()
    if not parts:
        return ""
    return parts[-1].lower().strip(string.punctuation)
