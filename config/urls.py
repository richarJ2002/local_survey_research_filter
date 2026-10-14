"""
Route Django admin and literature endpoints through the root URL table.

Manager/worker admission and prefix isolation are enforced by middleware,
not by changing database selection in this URL configuration.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   urls.py

@Brief:  Route Django admin and literature endpoints through the root URL table.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Custom library Imports
from django.contrib import admin
from django.urls import include, path

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

urlpatterns = [
    path('admin/', admin.site.urls),
    path("", include("literature.urls")),
]
