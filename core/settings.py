# core/settings.py
import os
from pathlib import Path
from dotenv import load_dotenv # Requisito: pip install python-dotenv
from django.core.exceptions import ImproperlyConfigured


#print("*****************************************")
#print("SISTEMA DARYZA: CARGANDO CONFIGURACIÓN...")
#print(f"RUTA ACTUAL: {os.getcwd()}")

# 1. Rutas del Proyecto
BASE_DIR = Path(__file__).resolve().parent.parent

# Cargar variables desde el archivo .env en la raíz
#load_dotenv(os.path.join(BASE_DIR, '.env'))


# Cambia la ruta para que busque DENTRO de la carpeta core
env_path = BASE_DIR / 'core' / '.env'
load_dotenv(dotenv_path=env_path)

# 2. Seguridad
# Sin fallback inseguro: si falta la variable de entorno, se falla fuerte y
# claro al arrancar en vez de seguir silenciosamente con un valor inseguro
# hardcodeado en el código fuente.
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY no está definida. Configúrala en core/.env antes de iniciar el servidor."
    )

# Por defecto DEBUG=False si la variable no está definida (fail-safe para
# despliegues donde se olvide configurarla explícitamente).
DEBUG = os.getenv('DEBUG', 'False') == 'True'

# Lista separada por comas en la variable de entorno; por defecto preserva
# el comportamiento local ya existente (localhost/127.0.0.1) si no se define.
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]


# 3. Definición de Aplicaciones
# Nota: Usamos el path completo apps.nombre para que Django las reconozca
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Librerías Externas
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders', # Requisito: pip install django-cors-headers

    # Tus Aplicaciones de Negocio
    'apps.base',
    'apps.sap_sync',
    'apps.appointments',
    'apps.operations',
    'apps.scheduling',    
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', # Para archivos estáticos
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware', # Debe ir antes de CommonMiddleware
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

# 4. Plantillas (Apuntando a tu carpeta templates en la raíz)
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')], # Ajustado a tu estructura
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

WSGI_APPLICATION = 'core.wsgi.application'

# 5. Base de Datos (PostgreSQL)
DB_PASSWORD = os.getenv('DB_PASSWORD')
if not DB_PASSWORD:
    raise ImproperlyConfigured(
        "DB_PASSWORD no está definida. Configúrala en core/.env antes de iniciar el servidor."
    )

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME', 'daryza_portal_db'),
        'USER': os.getenv('DB_USER', 'postgres'),
        'PASSWORD': DB_PASSWORD,
        'HOST': os.getenv('DB_HOST', '127.0.0.1'),
        'PORT': os.getenv('DB_PORT', '5432'),
    }
}

# 6. Internacionalización (Ajustado a Perú)
LANGUAGE_CODE = 'es-pe'
TIME_ZONE = 'America/Lima'
USE_I18N = True
USE_TZ = True

# 7. Archivos Estáticos y Media
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]

# Media para fotos de inspección y COA PDF
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Almacenamiento optimizado para producción
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# 8. Configuración de API REST
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ]
}

# 9. Autenticación LOGIN_REDIRECT_URL = '/appointments/portal/'
LOGIN_REDIRECT_URL = 'home_router'
LOGOUT_REDIRECT_URL = 'login'
LOGIN_URL = 'login'

ONEDRIVE_CLIENT_ID     = os.getenv('ONEDRIVE_CLIENT_ID', '')
ONEDRIVE_TENANT_ID     = os.getenv('ONEDRIVE_TENANT_ID', '')
ONEDRIVE_CLIENT_SECRET = os.getenv('ONEDRIVE_CLIENT_SECRET', '')
ONEDRIVE_DRIVE_ID      = os.getenv('ONEDRIVE_DRIVE_ID', '')
#print(f"VERIFICACIÓN AMBIENTE -> TENANT: {ONEDRIVE_TENANT_ID}")