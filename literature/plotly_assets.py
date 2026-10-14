#!/usr/bin/env python3

"""
Serve installed Plotly.js locally and embed it in standalone chart exports.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   plotly_assets.py

@Brief:  Serve installed Plotly.js locally and embed it in standalone chart exports.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

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


# ------------------------------------------------------------------------ #
# Definitions
# ------------------------------------------------------------------------ #

# --- Pinned-by-installation Plotly version for labels and tests ---
@functools.lru_cache(maxsize=1)
def plotly_version() -> str:
    """
    Read and cache the installed Plotly Python package version.

    Args:
        None

    Kwargs:
        None

    Returns:
        str: plotly.__version__, labelling the bundle's installation.
    """
    return plotly.__version__


# --- Read the installed bundle; no vendored copy, no npm, no CDN ---
@functools.lru_cache(maxsize=1)
def plotly_bundle() -> bytes:
    """
    Read and cache Plotly.js from the installed dependency, never a CDN.

    Args:
        None

    Kwargs:
        None

    Returns:
        bytes: Installed package_data/plotly.min.js, cached per process.

    Raises:
        OSError: If the installed bundle cannot be read.

    Side Effects:
        Read package resources on the first successful call; no DB access.
    """
    # ponytail: read from the installed package at request time; vendoring a
    # 5MB copy into git is the upgrade path nobody needs.
    return resources.files("plotly").joinpath("package_data/plotly.min.js").read_bytes()


# --- Inline bundle tag for self-contained standalone chart exports ---
def standalone_bundle_tag() -> str:
    """
    Embed the installed bundle and its third-party notice in a script tag.

    Args:
        None

    Kwargs:
        None

    Returns:
        str: Inline script with a licence comment and UTF-8-decoded bundle.

    Side Effects:
        Read the installed package resource if the bundle cache is empty.
    """
    return "<script>/* " + THIRD_PARTY_NOTICE + " */\n" + plotly_bundle().decode("utf-8") + "\n</script>"


# --- Local Plotly.js endpoint: research path so workers serve it prefixed ---
@require_safe
def plotly_js(request):
    """
    Serve installed Plotly.js with immutable caching and a version header.

    Args:
        request (HttpRequest): GET/HEAD request enforced by require_safe;
            query parameters do not select a different bundle.

    Kwargs:
        None

    Returns:
        HttpResponse: JavaScript bytes with a one-year immutable Cache-Control
            header and X-Plotly-Version; the decorator rejects unsafe methods.

    Side Effects:
        Read the installed bundle on a cache miss, never touch the database.
    """
    # ponytail: immutable content per installed version; the ?v= query is only
    # a cache-buster label, the bytes always match the running dependency.
    response = HttpResponse(
        plotly_bundle(), content_type="application/javascript")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["X-Plotly-Version"] = plotly_version()
    return response
