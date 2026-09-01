# apps/operations/services_entrada.py
"""
Sesión 99 — orquestación del paso humano intermedio de la Entrada de
Mercadería (GRPO), entre "Ticket FINALIZADO" y "el daemon la crea en SAP".

Mismo patrón que apps/invoicing/services_borrador.py (la Pantalla de
"Copiar de OC" de Factura): una orquestación de más alto nivel que un
candado atómico, separada de services.py.

Flujo:

  1. registrar_salida (services.py) sigue generando la EntradaMercaderia
     automáticamente al finalizar el Ticket — pero ahora en estado
     PENDIENTE / estado_sap='' (ver OperationsService._generar_entrada_
     mercaderia).

  2. Almacén / Materia Prima abre la pantalla de detalle, ajusta la
     cantidad real por línea si hace falta, y COMPLETA el número de LOTE
     (obligatorio) + fechas de vencimiento/fabricación (opcionales) —
     editar_linea_entrada, permitido solo mientras estado=PENDIENTE.

  3. enviar_a_sap: valida que TODAS las líneas tengan numero_lote, hace la
     transición PENDIENTE → ENVIADO + estado_sap='' → 'L'. Recién ahí el
     daemon la ve (GET /api/v1/entradas-pendientes/ filtra estado_sap='L').

  4. El daemon crea el GRPO real en SAP (POST /PurchaseDeliveryNotes, UN
     solo paso — sin cambios de la Etapa 4) y confirma vía confirmar-
     borrador / confirmar-definitivo, que ahora también guardan el DocNum
     visible (doc_num_sap). estado → CREADO_SAP.

Candado de permiso: el actor de recepción del Ticket (ALMACEN si la OC es
comercial, MATERIA_PRIMA si es Materia Prima — Ticket.es_materia_prima),
o un superusuario. Mismo criterio de "quién actúa" que
OperationsService._grupo_actor_recepcion, pero evaluado sobre el Ticket
ya FINALIZADO (grupo_requerido_por_etapa ya no aplica ahí).

Si SAP rechaza el documento después del envío (ej. un lote inválido para
un ítem específico), el daemon llama a reportar-error: se guarda
error_mensaje SIN tocar estado/estado_sap (la Entrada sigue en ENVIADO /
'L'). Para no dejarla atascada en un loop de reintento, el actor de
recepción puede REABRIRLA a PENDIENTE (reabrir_para_correccion) desde el
panel — sección "Entradas rechazadas por SAP" — o desde la propia
pantalla de detalle; corrige el lote/cantidad y la reenvía.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import EntradaMercaderia, EntradaMercaderiaEvento, EntradaMercaderiaLinea

# Centinela para editar_linea_entrada: distingue "el caller no pasó esta
# fecha" de "el caller pasó None a propósito para limpiarla".
_SIN_TOCAR = object()


def _grupo_actor(entrada: EntradaMercaderia) -> str:
    """ALMACEN o MATERIA_PRIMA según el tipo de OC del Ticket."""
    return 'MATERIA_PRIMA' if entrada.ticket.es_materia_prima else 'ALMACEN'


def puede_gestionar(entrada: EntradaMercaderia, usuario) -> bool:
    """True si `usuario` es el actor de recepción del Ticket (o superusuario)."""
    if usuario.is_superuser:
        return True
    return usuario.groups.filter(name=_grupo_actor(entrada)).exists()


def _validar_permiso(entrada: EntradaMercaderia, usuario) -> None:
    if not puede_gestionar(entrada, usuario):
        raise ValidationError(
            f"Esta Entrada de Mercadería debe gestionarla el grupo {_grupo_actor(entrada)}."
        )


def entradas_pendientes_para(usuario) -> list[EntradaMercaderia]:
    """
    EntradaMercaderia en estado PENDIENTE que le corresponden a `usuario`
    según su grupo (ALMACEN / MATERIA_PRIMA). Un superusuario ve todas.
    Devuelve una lista (no un queryset) porque el filtro por actor
    depende de Ticket.es_materia_prima, que no se puede expresar como un
    único `.filter()` de ORM — mismo criterio ya usado en
    _tickets_pendientes_materia_prima (volumen bajo de entradas
    PENDIENTE en este sistema).
    """
    qs = EntradaMercaderia.objects.filter(
        estado=EntradaMercaderia.ESTADO_PENDIENTE,
    ).select_related(
        'ticket__appointment__slot', 'ticket__appointment__user',
    ).prefetch_related(
        'lineas__po_line__purchase_order',
        'ticket__appointment__purchase_orders',
    ).order_by('fecha_generada')

    if usuario.is_superuser:
        return list(qs)

    grupos = set(usuario.groups.values_list('name', flat=True))
    return [e for e in qs if _grupo_actor(e) in grupos]


def entradas_rechazadas_para(usuario) -> list[EntradaMercaderia]:
    """
    EntradaMercaderia en estado ENVIADO con error_mensaje no vacío (SAP la
    rechazó y el daemon la reintenta sin éxito) que le corresponden a
    `usuario` — para que las reabra, corrija y reenvíe. Mismo filtrado por
    actor que entradas_pendientes_para.
    """
    qs = EntradaMercaderia.objects.filter(
        estado=EntradaMercaderia.ESTADO_ENVIADO,
    ).exclude(error_mensaje='').select_related(
        'ticket__appointment__slot', 'ticket__appointment__user',
    ).prefetch_related(
        'lineas__po_line__purchase_order',
        'ticket__appointment__purchase_orders',
    ).order_by('fecha_generada')

    if usuario.is_superuser:
        return list(qs)

    grupos = set(usuario.groups.values_list('name', flat=True))
    return [e for e in qs if _grupo_actor(e) in grupos]


def lineas_para_detalle(entrada: EntradaMercaderia) -> list[dict]:
    """
    Filas para la pantalla de detalle — cantidad real ya inspeccionada
    (editable), lote y fechas. Sin tocar la BD. `cantidad_oc` de
    referencia = po_line.quantity_sap (mismo criterio que la Pantalla de
    Copia de Factura).
    """
    filas = []
    for linea in entrada.lineas.select_related('po_line__purchase_order').order_by(
        'po_line__purchase_order_id', 'po_line__line_num'
    ):
        filas.append({
            'linea': linea,
            'po_line': linea.po_line,
            'cantidad_oc': linea.po_line.quantity_sap,
        })
    return filas


@transaction.atomic
def editar_linea_entrada(linea: EntradaMercaderiaLinea, usuario, *,
                         cantidad=None, numero_lote=None,
                         fecha_vencimiento_lote=_SIN_TOCAR,
                         fecha_fabricacion_lote=_SIN_TOCAR) -> EntradaMercaderiaLinea:
    """
    Edita cantidad / numero_lote / fechas de una línea mientras la Entrada
    siga en PENDIENTE. Actualiza SOLO los campos explícitamente provistos
    (None en cantidad/numero_lote = "no tocar"; _SIN_TOCAR en las fechas =
    "no tocar", None explícito = "limpiar la fecha").
    """
    entrada = linea.entrada
    # `linea.entrada` puede venir de una instancia cacheada — releemos el
    # estado real antes de decidir si la edición está permitida.
    entrada.refresh_from_db(fields=['estado'])
    _validar_permiso(entrada, usuario)
    if entrada.estado != EntradaMercaderia.ESTADO_PENDIENTE:
        raise ValidationError(
            "Esta Entrada de Mercadería ya fue enviada a SAP B1; no se puede editar."
        )

    campos = []
    if cantidad is not None:
        if cantidad <= 0:
            raise ValidationError("La cantidad debe ser mayor a 0.")
        linea.cantidad = cantidad
        campos.append('cantidad')
    if numero_lote is not None:
        linea.numero_lote = (numero_lote or '').strip()
        campos.append('numero_lote')
    if fecha_vencimiento_lote is not _SIN_TOCAR:
        linea.fecha_vencimiento_lote = fecha_vencimiento_lote
        campos.append('fecha_vencimiento_lote')
    if fecha_fabricacion_lote is not _SIN_TOCAR:
        linea.fecha_fabricacion_lote = fecha_fabricacion_lote
        campos.append('fecha_fabricacion_lote')

    if campos:
        linea.save(update_fields=campos)
    return linea


@transaction.atomic
def enviar_a_sap(entrada: EntradaMercaderia, usuario) -> EntradaMercaderia:
    """
    Transición PENDIENTE → ENVIADO (+ estado_sap='' → 'L'). Exige:
      - permiso del actor de recepción (o superusuario),
      - estado actual PENDIENTE,
      - al menos una línea,
      - numero_lote no vacío en TODAS las líneas.
    """
    entrada = EntradaMercaderia.objects.select_for_update().get(pk=entrada.pk)
    _validar_permiso(entrada, usuario)
    if entrada.estado != EntradaMercaderia.ESTADO_PENDIENTE:
        raise ValidationError("Esta Entrada de Mercadería ya fue enviada a SAP B1.")

    lineas = list(entrada.lineas.select_related('po_line').all())
    if not lineas:
        raise ValidationError("La Entrada de Mercadería no tiene ninguna línea.")

    # Sesión 99b: el lote es obligatorio SOLO para los ítems que SAP
    # gestiona por lote (PurchaseOrderLine.gestionado_por_lote). Una línea
    # que no lo requiere puede quedar sin numero_lote sin bloquear el
    # envío — y el daemon tampoco le mandará BatchNumbers.
    sin_lote = [
        l.po_line.item_code for l in lineas
        if l.po_line.gestionado_por_lote and not (l.numero_lote or '').strip()
    ]
    if sin_lote:
        raise ValidationError(
            "Complete el número de lote de las líneas gestionadas por lote antes de "
            f"enviar a SAP B1. Falta(n): {', '.join(sin_lote)}."
        )

    entrada.estado = EntradaMercaderia.ESTADO_ENVIADO
    entrada.estado_sap = 'L'
    entrada.error_mensaje = ''
    entrada.save(update_fields=['estado', 'estado_sap', 'error_mensaje'])
    return entrada


@transaction.atomic
def reabrir_para_correccion(entrada: EntradaMercaderia, usuario) -> EntradaMercaderia:
    """
    Vuelve una Entrada ENVIADO que SAP rechazó (error_mensaje no vacío) a
    PENDIENTE (+ estado_sap='L' → '', error_mensaje → '') para que el actor
    de recepción corrija el lote/cantidad y la reenvíe.

    NO se puede reabrir una Entrada ya CREADO_SAP (el GRPO real existe en
    SAP — reabrir ahí sería incorrecto).

    Deja RASTRO: crea un EntradaMercaderiaEvento(tipo=REABIERTA) con el
    error de SAP que se estaba corrigiendo + quién y cuándo — así aunque
    el error_mensaje se limpie, no se pierde de vista que falló una vez.
    """
    entrada = EntradaMercaderia.objects.select_for_update().get(pk=entrada.pk)
    _validar_permiso(entrada, usuario)
    if entrada.estado == EntradaMercaderia.ESTADO_CREADO_SAP:
        raise ValidationError(
            "El GRPO ya fue creado en SAP B1 — esta Entrada no se puede reabrir."
        )
    if entrada.estado != EntradaMercaderia.ESTADO_ENVIADO or not (entrada.error_mensaje or '').strip():
        raise ValidationError(
            "Solo se puede reabrir una Entrada que SAP haya rechazado."
        )

    EntradaMercaderiaEvento.objects.create(
        entrada=entrada,
        tipo=EntradaMercaderiaEvento.TIPO_REABIERTA,
        mensaje=entrada.error_mensaje,
        usuario=usuario,
    )

    entrada.estado = EntradaMercaderia.ESTADO_PENDIENTE
    entrada.estado_sap = ''
    entrada.error_mensaje = ''
    entrada.save(update_fields=['estado', 'estado_sap', 'error_mensaje'])
    return entrada
