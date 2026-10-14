"""
Provide independent scholarly API clients and canonical metadata mapping.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   __init__.py

@Brief:  Provide independent scholarly API clients and canonical metadata mapping.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

DEFAULT_SEARCH_PROVIDERS = (
    "openalex", "semantic_scholar", "crossref", "arxiv",
)
SEARCH_PROVIDERS = (*DEFAULT_SEARCH_PROVIDERS, "scopus")
