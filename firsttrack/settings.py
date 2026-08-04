from pathlib import Path
import os
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Wczytaj zmienne z pliku .env (jesli istnieje)
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'django-insecure-g_k*iqcol9rn2lmt=_tsyq=fzkemg((y34c66l)2k2u(btn1m3'
)

# Bezpieczny domyślny wybór: DEBUG=True tylko lokalnie (brak DATABASE_URL =
# SQLite deweloperskie), False wszędzie, gdzie skonfigurowano bazę produkcyjną
# (Azure). Z DEBUG=True strona błędu Django pokazuje m.in. hasło do bazy
# danych każdemu, kto trafi na wyjątek - nigdy nie może być włączone na żywym
# środowisku. Można nadpisać zmienną DEBUG w .env / App Settings, jeśli
# potrzeba (np. tymczasowe DEBUG=True do diagnozy - pamiętaj wyłączyć).
_env_debug = os.environ.get('DEBUG', '').strip().lower()
if _env_debug in ('1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'):
    DEBUG = _env_debug in ('1', 'true', 'yes', 'on')
else:
    DEBUG = not bool(os.environ.get('DATABASE_URL', '').strip())

ALLOWED_HOSTS = ['*']

# Django 4.0+ requires CSRF_TRUSTED_ORIGINS for HTTPS requests behind a proxy.
# Azure sets WEBSITE_HOSTNAME automatically (e.g. myapp.azurewebsites.net).
_HOSTNAME = os.environ.get('WEBSITE_HOSTNAME', '').strip()
CSRF_TRUSTED_ORIGINS = [f'https://{_HOSTNAME}'] if _HOSTNAME else []

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'productions',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'firsttrack.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'firsttrack.wsgi.application'

# PostgreSQL if DATABASE_URL is set, e.g.
# postgresql://user:password@host.postgres.database.azure.com/dbname?sslmode=require
# (Azure App Service → Configuration → App settings), otherwise local SQLite.
# The raw URL (with the password embedded) is deleted right after parsing so
# it can never end up as a module-level setting shown on a Django debug page -
# Django's secret-value filter only hides keys matching PASSWORD/SECRET/etc.
# by name, and a stray "_DATABASE_URL" variable doesn't match that pattern.
_database_url = os.environ.get('DATABASE_URL', '').strip()
if _database_url:
    _url = urlparse(_database_url)
    _sslmode = parse_qs(_url.query).get('sslmode', ['require'])[0]
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'HOST': _url.hostname,
            'PORT': _url.port or 5432,
            'NAME': _url.path.lstrip('/'),
            'USER': _url.username,
            'PASSWORD': _url.password,
            'OPTIONS': {'sslmode': _sslmode},
        }
    }
    del _url
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
del _database_url

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'

# Logowanie po numerze chip (ChipNumberBackend); ModelBackend zostaje dla
# panelu /admin/ (login + numer chip zapisany jako hasło użytkownika).
AUTHENTICATION_BACKENDS = [
    'productions.auth_backends.ChipNumberBackend',
    'django.contrib.auth.backends.ModelBackend',
]

LANGUAGE_CODE = 'pl'
TIME_ZONE = 'Europe/Warsaw'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Azure OpenAI – klucze z pliku .env
AZURE_OPENAI_KEY = os.environ.get('AZURE_OPENAI_KEY', '')
AZURE_OPENAI_ENDPOINT = os.environ.get('AZURE_OPENAI_ENDPOINT', '')
AZURE_OPENAI_DEPLOYMENT = os.environ.get('AZURE_OPENAI_DEPLOYMENT', 'gpt-4o')

# Email – Azure Communication Services (Email Communication Service +
# Email Communication Services Domain). Bez skonfigurowanych danych
# dostępowych maile trafiają lokalnie do pliku (tryb deweloperski).
DEFAULT_FROM_EMAIL = 'firsttrack@brueggen.com'
ACS_EMAIL_CONNECTION_STRING = os.environ.get('ACS_EMAIL_CONNECTION_STRING', '')
ACS_EMAIL_SENDER_ADDRESS = os.environ.get('ACS_EMAIL_SENDER_ADDRESS', '')

if ACS_EMAIL_CONNECTION_STRING and ACS_EMAIL_SENDER_ADDRESS:
    EMAIL_BACKEND = 'productions.email_backend.AzureCommunicationEmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.filebased.EmailBackend'
    EMAIL_FILE_PATH = BASE_DIR / 'sent_emails'

# Adres aplikacji – dodawany do maili powiadamiających o nowych produkcjach
FIRSTTRACK_APP_URL = os.environ.get(
    'FIRSTTRACK_APP_URL',
    'https://firsttrackwilga-fubha2avbsakcsg4.polandcentral-01.azurewebsites.net/',
)
