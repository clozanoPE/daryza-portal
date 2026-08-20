# core/settings.py
import os
from pathlib import Path
from dotenv import load_dotenv # Requisito: pip install python-dotenv
import dj_database_url # Requisito: pip install dj-database-url
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
#
# os.getenv() lee SIEMPRE el entorno real del proceso (os.environ) — esto
# funciona igual en Railway/producción (donde la variable se inyecta
# directo al contenedor, sin ningún archivo .env) que en local. load_dotenv()
# (arriba) solo agrega una comodidad: si core/.env existe, precarga su
# contenido a os.environ antes de este punto; si no existe (como en
# Railway), no hace nada y no falla — os.getenv() sigue leyendo el entorno
# real de todos modos. El mensaje de abajo lo aclara explícitamente para no
# inducir a pensar que hace falta el archivo (sesión 63: confusión real).
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY no está definida como variable de entorno del "
        "proceso. En Railway/producción: configurala en Settings → "
        "Variables del servicio (no hace falta ningún archivo .env ahí). "
        "En desarrollo local: definila en core/.env o exportala en tu shell."
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
    'apps.invoicing',
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
#
# Detecta automáticamente cuál de las 2 formas de configurar la conexión
# está presente, en vez de asumir una sola:
#   - DATABASE_URL: una sola variable con toda la cadena de conexión
#     (postgres://user:pass@host:port/dbname) — lo que expone el addon de
#     Postgres de Railway (y Heroku). Si está definida, tiene prioridad.
#   - Variables individuales DB_NAME/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT
#     (comportamiento local ya existente, sin cambios) — se usa cuando NO
#     hay DATABASE_URL, típicamente en desarrollo local vía core/.env.
DATABASE_URL = os.getenv('DATABASE_URL')

if DATABASE_URL:
    DATABASES = {
        # conn_max_age reutiliza conexiones entre requests (pool simple) —
        # razonable en un contenedor de vida corta como Railway.
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600)
    }
else:
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    if not DB_PASSWORD:
        raise ImproperlyConfigured(
            "Falta la configuración de base de datos como variables de "
            "entorno del proceso: definí DATABASE_URL (formato Railway/"
            "Heroku) o DB_PASSWORD junto con el resto de variables DB_* "
            "individuales. En Railway: Settings → Variables del servicio. "
            "En desarrollo local: core/.env o exportadas en tu shell."
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

# 10. Seguridad detrás de un proxy HTTPS (Railway u otro PaaS equivalente)
#
# CSRF_TRUSTED_ORIGINS: orígenes completos (con esquema) desde los que se
# aceptan POST con verificación CSRF pasada — Django lo exige explícitamente
# desde la 4.0 para cualquier origen público. Variable CSRF_TRUSTED_ORIGINS,
# CSV, ej: "https://daryza-portal.up.railway.app,https://mi-dominio.pe".
# Vacío por defecto: en local (http://localhost) no hace falta.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# Railway (como la mayoría de PaaS) termina TLS en su borde y reenvía el
# tráfico en texto plano hacia el contenedor, marcando el esquema original
# en el header X-Forwarded-Proto. Sin esto, Django nunca detecta HTTPS
# detrás del proxy — afecta request.is_secure(), las cookies "Secure" de
# abajo, y la propia verificación de CSRF_TRUSTED_ORIGINS. Seguro de dejar
# siempre activo: en local, sin proxy, ese header simplemente no llega en
# la request y Django sigue viendo la conexión como no seguros (correcto,
# http://localhost no tiene TLS).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Solo se activan fuera de DEBUG local — evita romper http://localhost
# (sin TLS, sin proxy que ponga X-Forwarded-Proto) mientras se desarrolla.
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG