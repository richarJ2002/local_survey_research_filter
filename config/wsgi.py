"""
Expose Django's WSGI application for the configured process role.

Import selects config.settings if unset and initializes the module-level
``application`` callable through Django's WSGI factory.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   wsgi.py

@Brief:  Expose Django's WSGI application for the configured process role.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import os

# Custom library Imports
from django.core.wsgi import get_wsgi_application

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

application = get_wsgi_application()
