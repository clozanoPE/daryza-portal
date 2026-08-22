# apps/base/models.py
from django.conf import settings
from django.db import models

class TimeStampedModel(models.Model):
    """
    Clase abstracta para heredar campos de auditoría (creado/actualizado).
    Útil para trazabilidad en auditorías de Daryza.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Sede(TimeStampedModel):
    """
    Planta/almacén físico de Daryza. Reemplaza al CharField con choices
    que tenía Appointment.lugar_entrega — permite que Appointment y
    AppointmentSlot referencien la misma sede real por FK, en vez de un
    choice de texto sin relación con ningún otro dato del sistema.

    Vive en apps.base (no en apps.appointments) porque se referencia desde
    más de una app de negocio (appointments hoy; operations/scheduling
    potencialmente más adelante), mismo criterio ya usado para
    apps/base/filters.py y apps/base/reporting.py.

    sap_whs_code queda nullable a propósito: la sincronización de OCs desde
    SAP (apps.sap_sync) no trae ningún dato de almacén/WhsCode hoy — el
    campo se deja listo para cuando exista esa sincronización real, sin
    bloquear su uso mientras tanto.
    """
    codigo = models.CharField(
        max_length=20, unique=True,
        help_text="Código corto interno, ej. 'LURIN'."
    )
    nombre = models.CharField(
        max_length=100,
        help_text="Nombre visible para proveedores y staff, ej. 'Planta Lurín'."
    )
    sap_whs_code = models.CharField(
        max_length=20, null=True, blank=True,
        help_text="Código de almacén (WhsCode) en SAP. Sin sincronización real todavía."
    )
    activa = models.BooleanField(
        default=True,
        help_text="Solo las sedes activas se ofrecen al agendar."
    )

    def __str__(self):
        return self.nombre

    class Meta:
        verbose_name = "Sede"
        verbose_name_plural = "Sedes"
        ordering = ['nombre']


class SupplierProfile(TimeStampedModel):
    """
    Versión mínima del perfil de proveedor — desbloquea `Factura.proveedor`
    (apps.invoicing) sin depender de `auth.User` directo. Vive en
    `apps.base` por el mismo motivo que `Sede`/`filters.py`/`reporting.py`:
    lo consumen más de una app de negocio (`apps.sap_sync`, que lo crea/
    actualiza en cada sync de OC — ver `apps/base/supplier_sync.py` — y
    `apps.invoicing`, que lo referencia).

    `user` es NULLABLE a propósito: un `SupplierProfile` nace del sync de
    OC (sin ninguna cuenta de Portal todavía) y se vincula a un `User`
    real recién en una fase futura de activación (Microsoft Graph
    Mail.Send, panel de administración — explícitamente NO parte de esta
    sesión). `on_delete=SET_NULL`: el perfil (y cualquier Factura que lo
    referencie con PROTECT) sobrevive si la cuenta de Portal se elimina.

    `ruc` se puebla con el mismo valor que `sap_card_code` al sincronizar
    (ver `supplier_sync.py`) — el propio código del proyecto ya trata
    `card_code`/`username` como "ruc_proveedor" sin quitarle ningún
    prefijo (`apps/appointments/views.py::PortalProveedorView`, línea
    `ruc_proveedor = self.request.user.username`), así que no hay
    evidencia de ninguna transformación adicional necesaria — si SAP
    codifica el RUC real de otra forma, corregirlo es una tarea de una
    fase futura con panel de administración, no algo para asumir aquí.
    """
    ESTADO_ACTIVO = 'ACTIVO'
    ESTADO_INACTIVO = 'INACTIVO'
    ESTADO_SUSPENDIDO = 'SUSPENDIDO'
    ESTADO_CHOICES = [
        (ESTADO_ACTIVO, 'Activo'),
        (ESTADO_INACTIVO, 'Inactivo'),
        (ESTADO_SUSPENDIDO, 'Suspendido'),
    ]

    ruc = models.CharField(max_length=50, blank=True, default='')
    razon_social = models.CharField(max_length=255, blank=True, default='')
    correo_electronico = models.CharField(max_length=255, blank=True, default='')
    sap_card_code = models.CharField(
        max_length=50, unique=True,
        help_text="CardCode de SAP (mismo valor que PurchaseOrder.card_code).",
    )
    estado = models.CharField(max_length=15, choices=ESTADO_CHOICES, default=ESTADO_ACTIVO)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='supplier_profile',
        help_text="Cuenta de Portal vinculada. Nulo hasta que exista un flujo de activación.",
    )

    def __str__(self):
        return f"{self.razon_social or self.sap_card_code} [{self.estado}]"

    class Meta:
        verbose_name = "Perfil de Proveedor"
        verbose_name_plural = "Perfiles de Proveedor"
        ordering = ['razon_social']