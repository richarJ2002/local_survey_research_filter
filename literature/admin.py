#!/usr/bin/env python3

'''
@File:   admin.py

@Brief:  Admin registration for audit-friendly browsing.

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

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


class ReadOnlyAuditAdmin(admin.ModelAdmin):
    """Browse search provenance without exposing alternate business writers."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Require the owning confirmation workflow for new records.

        Args:
            request: Admin request.

        Kwargs:
            None

        Returns:
            bool: False; this admin is for browsing only.
        """
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Model | None = None,
    ) -> bool:
        """Keep settings and saved provenance immutable in admin.

        Args:
            request: Admin request.

        Kwargs:
            obj: Selected record. Defaults to None for the collection.

        Returns:
            bool: False; edits belong to the owning workflow.
        """
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Model | None = None,
    ) -> bool:
        """Prevent deletion from erasing historical audit references.

        Args:
            request: Admin request.

        Kwargs:
            obj: Selected record. Defaults to None for the collection.

        Returns:
            bool: False; historical records remain available.
        """
        return False


@admin.register(Search)
class SearchAdmin(ReadOnlyAuditAdmin):
    """Search admin."""

    list_display = ("name", "project", "is_current", "query", "last_run_at")


@admin.register(SearchRun)
class SearchRunAdmin(ReadOnlyAuditAdmin):
    """SearchRun audit admin."""

    list_display = ("search", "source", "status",
                    "results_count", "no_abstract_count", "executed_at")


@admin.register(Paper)
class PaperAdmin(admin.ModelAdmin):
    """Paper admin."""

    list_display = ("title", "publication_year", "venue", "doi")
    search_fields = ("title", "doi")


@admin.register(Discovery)
class DiscoveryAdmin(ReadOnlyAuditAdmin):
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
