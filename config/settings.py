"""
Configure Django's fixed database role, local providers and worker cookies.

Implicit developer checks default to memory. Manager/worker startup requires
an explicit database; legacy disk state is available only through opt-in.
Explicit DB/legacy selection may load the configured credential file unless
dotenv is disabled. Signed-cookie sessions and worker UUID/epoch cookie paths
keep manager/worker session state out of research databases.

Copyright (c) 2026 Aerospace Engineering Research & Operation Systems
(AERO-Sys) group.

@File:   settings.py

@Brief:  Configure Django's fixed database role, local providers and worker cookies.

@Date:   14/10/2026
"""

# ------------------------------------------------------------------------ #
# Imports
# ------------------------------------------------------------------------ #

# Regular library imports
import os
from pathlib import Path

# ------------------------------------------------------------------------ #
# Module setup
# ------------------------------------------------------------------------ #

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv optional in some contexts
    def load_dotenv(*args: object, **kwargs: object) -> bool:
        """
        Leave environment variables unchanged when python-dotenv is absent.

        Args:
            *args (object): Ignored positional dotenv arguments, such as a
                credential-file path supplied by this module.

        Kwargs:
            **kwargs (object): Ignored dotenv options; no keys are processed.

        Returns:
            bool: Always False because no environment file is loaded.
        """
        return False

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

LOCAL_SURVEY_CREDENTIALS_FILE = Path(
    os.getenv("LOCAL_SURVEY_CREDENTIALS_FILE") or BASE_DIR / ".env")
# Implicit checks/tests must not read legacy credentials or select legacy truth.
if os.getenv("LOCAL_SURVEY_DB") or os.getenv("LOCAL_SURVEY_LEGACY_SHARED_DB") == "1":
    load_dotenv(LOCAL_SURVEY_CREDENTIALS_FILE)


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-local-only-k9^7eoy6o(=-o8e!t)61f4x56yo-(dggfb7ch^8=-_4*&n!0i!",
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv("DEBUG", "1") == "1"

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'literature',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'literature.worker.WorkerBindingMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

# Legacy truth is reachable only by explicit developer/rollback opt-in.
_DEFAULT_DB = ":memory:"
if os.getenv("WP_ROLE", "developer") == "developer":
    PROJECTS_DIR = BASE_DIR / "projects"
    _DEFAULT_DB = (
        PROJECTS_DIR / "db.sqlite3"
        if os.getenv("LOCAL_SURVEY_LEGACY_SHARED_DB") == "1"
        else ":memory:"
    )
else:
    if not os.getenv("LOCAL_SURVEY_DB"):
        raise RuntimeError(
            "manager/worker requires an explicit LOCAL_SURVEY_DB; no legacy fallback")
    PROJECTS_DIR = Path(os.environ["LOCAL_SURVEY_DB"]).parent
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.getenv("LOCAL_SURVEY_DB") or _DEFAULT_DB,
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Local Survey Research Filter: provider credentials (local only, never in DB).
OPENALEX_EMAIL = os.getenv("OPENALEX_EMAIL", "")
SEMANTIC_SCHOLAR_KEY = os.getenv("SEMANTIC_SCHOLAR_KEY", "")
OPENCITATIONS_TOKEN = os.getenv("OPENCITATIONS_TOKEN", "")
ELSEVIER_API_KEY = os.getenv("ELSEVIER_API_KEY", "")
ELSEVIER_INSTTOKEN = os.getenv("ELSEVIER_INSTTOKEN", "")

# WP01.03 worker boundary: immutable manager/worker roles fixed before setup.
# Signed-cookie sessions in manager/worker processes keep django_session/auth
# rows out of research stores; developer keeps the legacy DB session backend.
WP_ROLE = os.getenv("WP_ROLE", "developer")
WP_PROJECT_UUID = os.getenv("WP_PROJECT_UUID", "")
WP_WORKER_EPOCH = os.getenv("WP_WORKER_EPOCH", "")
if WP_ROLE in ("manager", "worker"):
    SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
if WP_ROLE == "worker" and WP_PROJECT_UUID and WP_WORKER_EPOCH:
    # ponytail: distinct names+paths per store epoch; browsers scope cookies,
    # ports do not. Validation stays server-side in WorkerBindingMiddleware.
    _WP_SHORT = WP_PROJECT_UUID.replace("-", "")[:8]
    SESSION_COOKIE_NAME = f"wpsess_{_WP_SHORT}"
    CSRF_COOKIE_NAME = f"wpcsrft_{_WP_SHORT}"
    SESSION_COOKIE_PATH = f"/p/{WP_PROJECT_UUID}/{WP_WORKER_EPOCH}/"
    CSRF_COOKIE_PATH = f"/p/{WP_PROJECT_UUID}/{WP_WORKER_EPOCH}/"
elif WP_ROLE == "manager":
    SESSION_COOKIE_NAME = "mgrsessionid"
    CSRF_COOKIE_NAME = "mgrcsrftoken"
