#!/usr/bin/env python3

'''
@File:   admin.py

@Brief:  Admin registration for audit-friendly browsing.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.contrib import admin

from literature.models import (
    Discovery,
    Paper,
    Project,
    ScreeningDecision,
    Search,
    SearchRun,
    Stage,
)

# ------------------------------------------------------------------------ #
# Admin
# ------------------------------------------------------------------------ #


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """Project admin."""

    list_display = ("name", "created_at")


@admin.register(Search)
class SearchAdmin(admin.ModelAdmin):
    """Search admin."""

    list_display = ("name", "project", "query", "last_run_at")


@admin.register(SearchRun)
class SearchRunAdmin(admin.ModelAdmin):
    """SearchRun audit admin."""

    list_display = ("search", "source", "status", "results_count", "no_abstract_count", "executed_at")


@admin.register(Paper)
class PaperAdmin(admin.ModelAdmin):
    """Paper admin."""

    list_display = ("title", "publication_year", "venue", "doi")
    search_fields = ("title", "doi")


@admin.register(Discovery)
class DiscoveryAdmin(admin.ModelAdmin):
    """Discovery provenance admin."""

    list_display = ("paper", "source", "search", "retrieved_at")


@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    """Stage admin."""

    list_display = ("project", "name", "order", "is_final")


@admin.register(ScreeningDecision)
class ScreeningDecisionAdmin(admin.ModelAdmin):
    """Decision audit admin."""

    list_display = ("paper", "stage", "decision", "decided_at")
