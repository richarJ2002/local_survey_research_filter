#!/usr/bin/env python3

'''
@File:   plotly_assets.py

@Brief:  Serve the installed Plotly.js bundle locally so charts work offline.

@Date:   03/10/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

import functools
from importlib import resources

# Custom library Imports
import plotly
from django.http import HttpResponse
from django.views.decorators.http import require_safe

# ------------------------------------------------------------------------ #
# Installed bundle (WP01.07)
# ------------------------------------------------------------------------ #

# --- Third-party notice kept with every local/standalone use ---
THIRD_PARTY_NOTICE = (
    "Charts render with Plotly.js (MIT licence, Copyright 2012-2026 Plotly, Inc.) "
    "from the installed Plotly Python package (MIT licence, "
    "Copyright 2016-2026 Plotly Technologies Inc.)."
)


# --- Pinned-by-installation Plotly version for labels and tests ---
@functools.lru_cache(maxsize=1)
def plotly_version() -> str:
    """Installed Plotly Python version; the JS bundle ships with it."""
    return plotly.__version__


# --- Read the installed bundle; no vendored copy, no npm, no CDN ---
@functools.lru_cache(maxsize=1)
def plotly_bundle() -> bytes:
    """Plotly.js bytes from the installed dependency (cached per process)."""
    # ponytail: read from the installed package at request time; vendoring a
    # 5MB copy into git is the upgrade path nobody needs.
    return resources.files("plotly").joinpath("package_data/plotly.min.js").read_bytes()


# --- Inline bundle tag for self-contained standalone chart exports ---
def standalone_bundle_tag() -> str:
    """One inline <script> with the bundle plus its licence notice."""
    return "<script>/* " + THIRD_PARTY_NOTICE + " */\n" + plotly_bundle().decode("utf-8") + "\n</script>"


# --- Local Plotly.js endpoint: research path so workers serve it prefixed ---
@require_safe
def plotly_js(request):
    """Serve the installed Plotly.js bundle; safe GET, never touches the DB."""
    # ponytail: immutable content per installed version; the ?v= query is only
    # a cache-buster label, the bytes always match the running dependency.
    response = HttpResponse(plotly_bundle(), content_type="application/javascript")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["X-Plotly-Version"] = plotly_version()
    return response
