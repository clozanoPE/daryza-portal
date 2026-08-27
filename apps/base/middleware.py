# apps/base/middleware.py
"""
Etapa 2.4 (Proveedores, sesión 89) — barrera real de "primer acceso con
credencial temporal": intercepta TODA request de un usuario autenticado
cuyo `SupplierProfile.debe_cambiar_password` sea `True`, sin importar
si llega justo tras el redirect post-login o por una URL profunda ya
con sesión iniciada de antes.

Corrección explícita de un diseño anterior descartado en el chat de
esta misma sesión de trabajo: NO basta con aplicar este chequeo en
`redirect_by_role` (solo se ejecuta en el momento exacto del redirect
inicial post-login) — un usuario que navegue directo a una URL ya
conocida (pestaña abierta de antes, link guardado) en una sesión
posterior nunca vuelve a pasar por ahí. Un middleware, que corre en
TODA request, es el único punto que garantiza cobertura completa sin
tener que agregar el mismo chequeo a cada vista de proveedor por
separado — están mezcladas entre clases (`PortalProveedorView`,
`HistorialCitasView`) y funciones (`nueva_factura_ocs`, `mis_facturas`,
etc.), sin ninguna base compartida sobre la que enganchar un solo punto
de control.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class ForzarCambioPasswordMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response
        # Calculadas una sola vez (el middleware se instancia una vez
        # por proceso, después de que el URLconf ya está cargado) — no
        # en cada request.
        self._url_cambio_password = reverse('cambiar_password_obligatorio')
        self._url_logout = reverse('logout')
        self._prefijo_static = '/' + settings.STATIC_URL.lstrip('/')
        self._prefijo_media = '/' + settings.MEDIA_URL.lstrip('/')

    def __call__(self, request):
        if self._debe_interceptar(request):
            return redirect('cambiar_password_obligatorio')
        return self.get_response(request)

    def _debe_interceptar(self, request):
        if not request.user.is_authenticated:
            return False

        if self._ruta_excluida(request.path):
            return False

        # getattr con default: un OneToOneField inverso sin objeto
        # relacionado lanza RelatedObjectDoesNotExist al accederlo
        # directo (a diferencia de en un template, donde Django lo
        # resuelve como fallo silencioso) — pero esa excepción hereda
        # también de AttributeError a propósito (diseño de Django para
        # este caso exacto), así que getattr(..., None) la captura
        # limpio, sin try/except explícito. Cubre tanto al staff interno
        # (nunca tiene SupplierProfile) como a un superusuario.
        perfil = getattr(request.user, 'supplier_profile', None)
        if perfil is None:
            return False

        return bool(perfil.debe_cambiar_password)

    def _ruta_excluida(self, path):
        # La vista de cambio en sí (sin esto, se redirigiría a sí misma
        # en loop infinito) y el logout (sin esto, un proveedor con el
        # flag en True no podría ni siquiera cerrar sesión).
        if path in (self._url_cambio_password, self._url_logout):
            return True
        # Estáticos/media: nunca requieren ni deben pasar por este
        # chequeo — interceptarlos rompería el propio render de
        # cambiar_password_obligatorio.html (CSS/JS/logo).
        if path.startswith(self._prefijo_static) or path.startswith(self._prefijo_media):
            return True
        return False
