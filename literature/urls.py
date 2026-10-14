#!/usr/bin/env python3

'''
@File:   urls.py

@Brief:  URL routes for literature app (projects, search, screening, export).

@Date:   28/09/2026
@Copyright: 2026 Aerospace Engineering Research & Operation Systems (AERO-Sys) group.

'''

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
from __future__ import annotations

# Custom library Imports
from django.urls import path

from literature import chart_views, plotly_assets, survey_views, views, workspace_views

# ------------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------------ #

urlpatterns = [
    path("", views.project_list, name="project_list"),
    path("workspace/", workspace_views.workspace_home, name="workspace_home"),
    path("workspace/set-root/", workspace_views.workspace_set_root,
         name="workspace_set_root"),
    path("workspace/new/", workspace_views.workspace_new, name="workspace_new"),
    path("workspace/open/", workspace_views.workspace_open, name="workspace_open"),
    path("workspace/operation/", workspace_views.workspace_operation,
         name="workspace_operation"),
    path("project/new/", views.project_create, name="project_create"),
    path("project/<int:pk>/", views.project_detail, name="project_detail"),
    path("project/<int:pk>/name/", views.project_name, name="project_name"),
    path("project/<int:pk>/lifecycle/<str:action>/",
         workspace_views.project_lifecycle, name="project_lifecycle"),
    path("project/<int:pk>/delete/", views.project_delete, name="project_delete"),
    path("project/<int:pk>/papers/", views.papers_view, name="papers_view"),
    path("project/<int:pk>/papers/add/", views.paper_add, name="paper_add"),
    path("project/<int:pk>/papers/lookup/",
         views.paper_lookup, name="paper_lookup"),
    path("project/<int:pk>/papers/<int:paper_id>/",
         views.paper_manage, name="paper_manage"),
    path("project/<int:pk>/search/new/",
         views.search_create, name="search_create"),
    path("project/<int:pk>/refs/backfill/",
         views.refs_backfill, name="refs_backfill"),
    path("project/<int:pk>/citations/enrich/",
         views.citations_enrich, name="citations_enrich"),
    path(
        "project/<int:pk>/search/<int:search_id>/edit/",
        views.search_update,
        name="search_update",
    ),
    path(
        "project/<int:pk>/search/<int:search_id>/run/",
        views.search_run,
        name="search_run",
    ),
    path("settings/", views.settings_view, name="settings_view"),
    path(
        "project/<int:pk>/screen/<str:stage_name>/",
        views.screening_view,
        name="screening_view",
    ),
    path("screening/decide/", views.screening_decide, name="screening_decide"),
    path("project/<int:pk>/references/review/",
         views.reference_review, name="reference_review"),
    path(
        "project/<int:pk>/screen/complete/<str:stage_name>/",
        views.screening_complete,
        name="screening_complete",
    ),
    path(
        "project/<int:pk>/export/markdown/",
        views.export_markdown_view,
        name="export_markdown",
    ),
    path("project/<int:pk>/references/<str:group>/download/",
         views.references_download, name="references_download"),
    path("project/<int:pk>/plots/", views.plots_view, name="plots_view"),
    path("project/plotly.min.js", plotly_assets.plotly_js, name="plotly_js"),
    path("project/<int:pk>/plots/entries/",
         chart_views.chart_entries, name="chart_entries"),
    path("project/<int:pk>/revision/",
         chart_views.project_revision, name="project_revision"),
    path("project/<int:pk>/survey/",
         survey_views.survey_graph, name="survey_graph"),
    path("project/<int:pk>/survey/add/",
         survey_views.survey_entry, name="survey_entry_add"),
    path("project/<int:pk>/survey/<int:entry_id>/edit/",
         survey_views.survey_entry, name="survey_entry_edit"),
    path("project/<int:pk>/plots/download/",
         views.plots_download, name="plots_download"),
    path("project/<int:pk>/duplicates/",
         views.duplicates_view, name="duplicates_view"),
    path(
        "project/<int:pk>/duplicates/merge/",
        views.duplicates_merge,
        name="duplicates_merge",
    ),
    path(
        "project/<int:pk>/duplicates/remove/",
        views.duplicates_remove,
        name="duplicates_remove",
    ),
]
