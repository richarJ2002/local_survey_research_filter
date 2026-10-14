"""
Search Scopus with header-only credentials and bounded STANDARD pages.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File: scopus.py
@Brief: Optional Elsevier search; no abstract or reference retrieval.
@Date: 13/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import time
from dataclasses import dataclass
from math import isfinite

# Custom library Imports
import httpx
from django.conf import settings

from .mapping import map_scopus_to_canonical

# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Query and canonical result contracts ---


@dataclass(frozen=True)
class ScopusConfig:
    """
    Hold search text, OR-matched rows, year bounds and page ceilings.

    Empty keyword rows use query as one literal phrase. Venue/author groups
    are ANDed with keywords, with OR within each group. Defaults request
    25 STANDARD records on at most four pages; offset pagination never
    crosses the API's 5,000-result ceiling.
    Scopus quotes denote loose phrases, not exact matching: punctuation
    can be ignored, wildcards remain active and AUTH matches surnames/initials.
    """

    query: str
    year_min: int | None = None
    year_max: int | None = None
    keywords: tuple[str, ...] = ()
    venues: tuple[str, ...] = ()
    author_names: tuple[str, ...] = ()
    page_size: int = 25
    max_pages: int = 4


@dataclass(frozen=True)
class ProviderResult:
    """
    Pair canonical paper metadata with its original Scopus search entry.

    The search service owns persistence; this result has no credentials.
    """

    paper_data: dict
    raw_response: dict


class ScopusError(RuntimeError):
    """
    Report access, query, quota or response failures without secret echoes.
    """


class ScopusSource:
    """
    Fetch opt-in Scopus metadata without saving papers or logging secrets.
    """

    # ponytail: STANDARD offset pages only; add cursor/detail APIs if needed.
    source_key = "scopus"
    base_url = "https://api.elsevier.com/content/search/scopus"

    def __init__(
        self, api_key: str | None = None, insttoken: str | None = None,
    ) -> None:
        """
        Select explicit credentials or the fixed startup settings.

        Args:
            None

        Kwargs:
            api_key: Elsevier API key; None reads ELSEVIER_API_KEY.
                An explicit blank disables access. Defaults to None.
            insttoken: Optional institutional token; None reads
                ELSEVIER_INSTTOKEN. Defaults to None.

        Returns:
            None
        """
        self.api_key = api_key if api_key is not None else getattr(
            settings, "ELSEVIER_API_KEY", "")
        self.insttoken = insttoken if insttoken is not None else getattr(
            settings, "ELSEVIER_INSTTOKEN", "")

    def default_headers(self) -> dict[str, str]:
        """
        Validate credentials and send them only in Elsevier headers.

        Args:
            None

        Kwargs:
            None

        Returns:
            dict[str, str]: JSON Accept and API key, plus a nonblank token.

        Raises:
            ScopusError: If the key is missing or credentials contain spaces,
                non-ASCII/control characters or exceed 4,096 characters.
        """
        if not self.api_key:
            raise ScopusError(
                "Add an Elsevier Scopus API key in manager Settings, "
                "restart the launcher, then retry.")
        for credential in (self.api_key, self.insttoken):
            if not isinstance(credential, str) or len(credential) > 4096 or any(
                not 33 <= ord(character) <= 126 for character in credential
            ):
                raise ScopusError(
                    "Invalid Elsevier credential; check manager Settings "
                    "and restart the launcher.")
        headers = {"Accept": "application/json", "X-ELS-APIKey": self.api_key}
        if self.insttoken:
            headers["X-ELS-Insttoken"] = self.insttoken
        return headers

    def build_request_params(self, config: ScopusConfig, offset: int) -> dict:
        """
        Translate literal rows to Scopus fields without exposing credentials.

        Args:
            config: Keywords, venue/author rows, optional years and page size.
            offset: Zero-based result offset, from 0 through 4,999.

        Kwargs:
            None

        Returns:
            dict: STANDARD query, count capped at 200, and start offset.

        Raises:
            ScopusError: If rows contain quotation/control characters, the
                query is empty, years are invalid, or pagination is invalid.
        """
        if (type(offset) is not int or not 0 <= offset < 5000
                or type(config.page_size) is not int or config.page_size < 1):
            raise ScopusError("Invalid Scopus page size or offset.")

        def quoted(value: str) -> str:
            """
            Quote a literal row without allowing query-syntax injection.

            Args:
                value: Nonblank user-entered row.

            Kwargs:
                None

            Returns:
                str: Quoted, stripped phrase for the Scopus query.

            Raises:
                ScopusError: If the row is malformed or contains quotes,
                    backslashes or control characters.
            """
            if not isinstance(value, str) or any(
                character in '"\\' or ord(character) < 32
                or ord(character) == 127
                for character in value
            ):
                raise ScopusError(
                    "Scopus search rows must not contain quotes, "
                    "backslashes or control characters.")
            value = value.strip()
            if not value:
                raise ScopusError("Scopus search needs nonblank rows.")
            return '"' + value + '"'

        keywords = config.keywords or (config.query,)
        clauses = []
        for field, rows in (
            ("TITLE-ABS-KEY", keywords), ("SRCTITLE", config.venues),
            ("AUTH", config.author_names),
        ):
            terms = [f"{field}({quoted(row)})" for row in rows if row]
            if field == "TITLE-ABS-KEY" and not terms:
                raise ScopusError("Scopus search needs a keyword.")
            if terms:
                clauses.append("(" + " OR ".join(terms) + ")")
        for year in (config.year_min, config.year_max):
            if year is not None and (
                type(year) is not int or not 1 <= year <= 9999
            ):
                raise ScopusError("Scopus years must be between 1 and 9999.")
        if (config.year_min is not None and config.year_max is not None
                and config.year_min > config.year_max):
            raise ScopusError("Scopus year bounds are reversed.")
        if config.year_min is not None:
            clauses.append(f"PUBYEAR > {config.year_min - 1}")
        if config.year_max is not None:
            clauses.append(f"PUBYEAR < {config.year_max + 1}")
        return {"query": " AND ".join(clauses), "view": "STANDARD",
                "count": min(config.page_size, 200, 5000 - offset),
                "start": offset}

    def _get_with_backoff(
        self, client: httpx.Client, params: dict,
    ) -> httpx.Response:
        """
        Try one page up to three times for transport, throttle or 5xx errors.

        Args:
            client: Borrowed HTTP client; never redirected to another host.
            params: Credential-free query parameters.

        Kwargs:
            None

        Returns:
            httpx.Response: A response requiring final status/payload checks.

        Raises:
            ScopusError: If retries exhaust or the weekly quota is exhausted.
            No response body or transport exception text enters the error.

        Side Effects:
            GET the fixed Scopus endpoint with header-only credentials;
            retry sleeps use exponential or numeric Retry-After seconds,
            capped at 30 seconds.
        """
        headers = self.default_headers()
        for attempt in range(3):
            try:
                response = client.get(
                    self.base_url, params=params, headers=headers,
                    timeout=30.0, follow_redirects=False)
            except httpx.RequestError:
                if attempt == 2:
                    raise ScopusError(
                        "Scopus is unreachable; check your connection "
                        "and retry.") from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429 and response.headers.get(
                    "X-ELS-Status") == "QUOTA_EXCEEDED":
                raise ScopusError(
                    "Scopus quota exhausted; wait for the quota reset "
                    "or contact Elsevier API support.")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == 2:
                    raise ScopusError(
                        "Scopus is rate-limited or unavailable after retries; "
                        "try again later.")
                try:
                    delay = float(response.headers.get(
                        "Retry-After", 2 ** attempt))
                except ValueError:
                    delay = 2 ** attempt
                if not isfinite(delay):
                    delay = 2 ** attempt
                time.sleep(max(0, min(delay, 30)))
                continue
            return response

    def fetch(
        self, config: ScopusConfig, client: httpx.Client | None = None,
    ) -> tuple[list[ProviderResult], int]:
        """
        Fetch bounded STANDARD pages and map bibliographic metadata only.

        Args:
            config: Search rows, filters, page size and page ceiling.

        Kwargs:
            client: Optional borrowed client. Defaults to None, creating
                a client closed by this method. Credentials are validated
                before creating a client or making any request.

        Returns:
            tuple[list[ProviderResult], int]: Canonical results and provider
            total before page/5,000-result truncation. Zero results is a valid
            success; access failures are never disguised as empty results.

        Raises:
            ScopusError: If credentials, access, query, response or retries
                fail. Errors never include request headers or response bodies.

        Side Effects:
            Request at most max(1, max_pages) pages, paced 0.4 seconds apart,
            without database writes or abstract/reference API calls.
        """
        self.default_headers()
        params = self.build_request_params(config, 0)
        if type(config.max_pages) is not int:
            raise ScopusError("Invalid Scopus page ceiling.")
        own_client = client is None
        http = client if client is not None else httpx.Client()
        results = []
        offset = 0
        total = 0
        try:
            for page in range(max(1, config.max_pages)):
                if page:
                    time.sleep(0.4)
                response = self._get_with_backoff(http, params)
                errors = {
                    400: "Scopus rejected the query; check your search rows.",
                    401: "Scopus authentication failed; check the Elsevier "
                    "API key in Settings and restart the launcher.",
                    403: "Scopus access denied; an account/API key alone "
                    "does not grant API entitlement. Check institutional "
                    "network access or an Elsevier institutional token.",
                }
                if response.status_code != 200:
                    raise ScopusError(errors.get(
                        response.status_code, "Scopus search request failed."))
                if len(response.content) > 2_000_000:
                    raise ScopusError("Scopus search response exceeds 2 MB.")
                try:
                    payload = response.json()
                    data = payload.get("search-results")
                    if not isinstance(data, dict):
                        raise TypeError
                    reported_total = data.get("opensearch:totalResults")
                    if isinstance(reported_total, bool) or not isinstance(
                        reported_total, (int, str)
                    ):
                        raise TypeError
                    reported_total = int(reported_total)
                    if reported_total < 0:
                        raise ValueError
                    if page == 0:
                        total = reported_total
                    if not total:
                        break
                    batch = data.get("entry")
                    if isinstance(batch, dict):
                        batch = [batch]
                    if not isinstance(batch, list) or not batch or len(
                            batch) > params["count"]:
                        raise ValueError
                    for entry in batch:
                        if not isinstance(entry, dict) or "error" in entry:
                            raise ValueError
                        paper_data, _ = map_scopus_to_canonical(entry)
                        results.append(ProviderResult(paper_data, entry))
                except (AttributeError, TypeError, ValueError):
                    raise ScopusError(
                        "Scopus returned an invalid search response.") from None
                offset += len(batch)
                if offset >= min(total, 5000):
                    break
                params = self.build_request_params(config, offset)
            return results, total
        finally:
            if own_client:
                http.close()
