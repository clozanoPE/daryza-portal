# apps/appointments/models.py
from django.conf import settings
from django.db import models
from apps.base.models import TimeStampedModel

class AppointmentSlot(TimeStampedModel): 
    date = models.DateField()
    start_time = models.TimeField()
    #end_time = models.TimeField()
    end_time = models.TimeField(null=True, blank=True)
    dock = models.CharField(max_length=50, help_text="Muelle/Puerto")
    max_capacity = models.PositiveIntegerField(default=1)
    is_full_override = models.BooleanField(default=False,
                                            help_text=(
                                                    "Si True, el slot no acepta nuevas citas aunque no haya llegado a la "
                                                    "capacidad máxima. Se activa manualmente desde el panel de admin o "
                                                    "automáticamente al confirmar una cita que llena el slot."
                                                )
    )

    def __str__(self):
        return f"{self.date} | {self.start_time} - {self.dock}"

    
    class Meta:
        # RESTRICCIÓN AÑADIDA: No puede haber dos slots para la misma fecha y hora.
        # Protege contra duplicados silenciosos que causarían doble reserva.
        # Requiere migración: 0003_appointmentslot_unique_date_starttime.py
        unique_together = [('date', 'start_time')]
        ordering = ['date', 'start_time']

class Appointment(TimeStampedModel):     
    """
    Representa una solicitud de cita de un proveedor para un AppointmentSlot.

    CICLO DE VIDA:
      SOLICITADO → CONFIRMADA (Almacén aprueba) → FINALIZADA (ingreso completado)
               ↘ RECHAZADO  (Almacén rechaza)
               ↘ CANCELADA  (proveedor o admin cancela)

    Al pasar a CONFIRMADA:
      - Se genera token_qr (código único que el proveedor presenta en planta)
      - Se crea el Ticket de inspección vinculado (ver apps/operations/models.py)
      - Se notifica al proveedor (ver AppointmentService._notificar_proveedor_qr)

    Campos heredados de TimeStampedModel: created_at, updated_at
    """
    SEDES = [
        ('LURIN', 'Planta Lurin'),
        ('PUNTA_NEGRA', 'Almacén Punta Negra'),
        ('DISTRIBUCION', 'Distribucion'),
    ]

    ESTADOS = [
        ('SOLICITADO', 'Solicitado'),
        ('CONFIRMADA', 'Confirmada'),
        ('RECHAZADO', 'Rechazado'),
        ('CANCELADA', 'Cancelada'),
        ('FINALIZADA', 'Finalizada (En Planta)'),
    ]

    slot = models.ForeignKey(AppointmentSlot, on_delete=models.CASCADE, related_name='appointments')
    
    # Relación ManyToMany con las OCs de SAP
    purchase_orders = models.ManyToManyField('sap_sync.PurchaseOrder') 
        
    # Relación con el Usuario (Proveedor) - Permite nulos para compatibilidad db_column='proveedor'   
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='mis_citas',
        null=True,   
        blank=True        
    )

    # Documentación y Control
    coa_pdf = models.FileField(upload_to='documentos/coa/%Y/%m/', null=True, blank=True, help_text="Certificado de Análisis. Obligatorio para autorizar el ingreso a planta.")
    
    # Cambiado max_length a 20 para soportar 'SOLICITADO' y 'CONFIRMADA' del service
    #status = models.CharField(max_length=20, default='SOLICITADO') 
    status = models.CharField(max_length=20, choices=ESTADOS, default='SOLICITADO')
    
    token_qr = models.CharField(max_length=100, unique=True, null=True, blank=True)     

    lugar_entrega = models.CharField(max_length=20, choices=SEDES, default='LURIN')
    observaciones_admin = models.TextField(null=True, blank=True)
    fecha_respuesta_admin = models.DateTimeField(null=True, blank=True, help_text="Timestamp de cuando Almacén confirmó o rechazó la cita.")

    coa_configurado = models.BooleanField(
        default=False,
        help_text=(
            "True desde que Compras guarda una configuración de COA por "
            "línea para esta cita (ajax_configurar_items_coa), aunque sea "
            "sin cambios. Mientras esté en False, AppointmentService."
            "preparar_detalle_compras puede sugerir requiere_coa=True por "
            "defecto en las líneas de OC Materia Prima al abrir la "
            "configuración; una vez en True, ya no vuelve a tocarlas, para "
            "no sobreescribir una decisión manual ya guardada."
        )
    )

    def __str__(self):
        # Manejo de nulo en user para evitar errores en el Admin si el dato es viejo
        username = self.user.username if self.user else "Sin Usuario"
        return f"Cita {self.id} - {username} - {self.slot.date}"