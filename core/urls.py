# core/urls.py
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include
from django.conf import settings             # <--- ESTA FALTA (Error actual)
from django.conf.urls.static import static   # <--- ESTA FALTABA ANTES
from apps.base.views import (  # <--- Importa el enrutador
    cambiar_password_obligatorio,
    confirmar_recuperacion,
    redirect_by_role,
    solicitar_recuperacion,
)

urlpatterns = [
    # Panel de administración de Django
    path('', redirect_by_role, name='home_router'),
    path('admin/', admin.site.urls),
    path('home/', redirect_by_role, name='home_router'),
    # Etapa 2.4 (Proveedores, sesión 89): destino del
    # ForzarCambioPasswordMiddleware — fuera de cualquier namespace de
    # app de negocio, mismo criterio ya usado para home_router/login/logout.
    path('cuenta/cambiar-password/', cambiar_password_obligatorio, name='cambiar_password_obligatorio'),
    # Etapa 2.5 (Proveedores, sesión 90): recuperación de contraseña —
    # mismo prefijo 'cuenta/' que la de arriba, ambas rutas de gestión
    # de credenciales fuera de cualquier namespace de app de negocio.
    path('cuenta/recuperar/', solicitar_recuperacion, name='solicitar_recuperacion'),
    path('cuenta/recuperar/<uidb64>/<token>/', confirmar_recuperacion, name='confirmar_recuperacion'),
    path('appointments/', include('apps.appointments.urls')),
    path('operations/',  include('apps.operations.urls')),   
    path('scheduling/',  include('apps.scheduling.urls')),
    path('invoicing/',   include('apps.invoicing.urls')),
    # Rutas de Autenticación
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    # Endpoints de la API (v1)
    # Esto busca el archivo api/urls.py y lo incluye bajo el prefijo /api/v1/
    path('api/v1/', include('api.urls')),
]

# Esto es lo que causaba el error por no tener el import de arriba
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
 