# apps/invoicing/services_borrador.py
"""
Sub-fase 3.4: orquestación de creación de Factura vía el patrón "Copiar de
OC(s)" (mismo patrón ya usado en SAP B1 — "Copy From"). Vive separado de
services.py (candados de negocio "atómicos" ya construidos en la Sub-fase
3.1: saldo_disponible / validar_oc_disponible / validar_retencion_
detraccion) por ser una orquestación de más alto nivel que los combina —
mismo criterio ya usado para separar services_archivos.py (carga de
archivos) de services.py.

3 momentos, sin escritura en BD hasta el tercero:

  1. listar_ocs_elegibles: qué OC puede elegir el proveedor logueado para
     armar una Factura nueva — Ticket FINALIZADO (ya tiene EntradaMercaderia,
     sesión 57), sin ninguna FacturaOrdenCompra activa vinculada, con al
     menos una línea ACTIVA con saldo_disponible > 0.

  2. lineas_para_copiar: líneas + saldo disponible de las OC ya
     seleccionadas por el proveedor, agrupadas por OC — para poblar la
     Pantalla de Copia SIN tocar la BD (solo lecturas vía saldo_
     disponible). Agrupado aquí en Python, no vía `{% regroup %}` en el
     template — ese tag exige que la fuente ya venga ordenada por la
     clave de agrupación, riesgo de fondo ya documentado en este mismo
     proyecto (CLAUDE.md, sesión 23) — se evita del todo agrupando de
     forma explícita antes de llegar al template.

  3. crear_factura_desde_ocs: el primer guardado real en BD — Factura +
     FacturaOrdenCompra (una por OC) + FacturaLinea (una por línea con
     cantidad > 0), snapshotting cantidad_oc/precio_oc. En 2 fases:

       Fase 1 (_crear_factura_y_lineas, @transaction.atomic): 100% DB, sin
       ninguna llamada de red — valida pertenencia de cada OC al
       proveedor, que todas las OC compartan la misma sede (Factura.sede
       es una única FK; mezclar sedes en una misma Factura no tiene
       sentido de negocio, mismo criterio ya aplicado a "no mezclar OC
       MP con comercial en una misma cita", sesión 28), y
       validar_oc_disponible (Sub-fase 3.1, con su candado de
       concurrencia real ya verificado — sesión 70) para CADA OC
       seleccionada, dentro del MISMO bloque atómico que crea los
       registros — así el lock de PurchaseOrder cubre hasta el commit.

       Fase 2 (_adjuntar_retencion_detraccion, FUERA del atomic de la
       fase 1): sube los documentos de retención/detracción reutilizando
       services_archivos.cargar_archivo_factura_linea — el mismo
       pipeline de validación/subida a OneDrive/hash de la Sub-fase 3.2,
       no una copia — y aplica el candado de InvoicingService.
       validar_retencion_detraccion (Sub-fase 3.1). Deliberadamente FUERA
       del bloque con locks: cargar_archivo_factura_linea hace una
       llamada de red real a Microsoft Graph, y no hay que mantener el
       lock de PurchaseOrder retenido durante esa llamada (ni ningún otro
       lock de fila, más lento e innecesario). Si la fase 2 falla (falta
       el documento, o el archivo no es válido), se borra la Factura
       completa (CASCADE limpia FacturaOrdenCompra/FacturaLinea) — "no
       crees una Factura parcial" (pedido explícito). Un documento que sí
       llegó a subirse a OneDrive antes de que fallara una línea
       posterior queda huérfano ahí (OneDriveClient no tiene ningún
       método de borrado, mismo límite ya aceptado en el resto del
       proyecto desde su creación — no es un problema nuevo de esta
       sesión).

  4-5. editar_cabecera_factura / editar_linea_factura: edición mientras
     la Factura sigue en BORRADOR/OBSERVADA — mismo candado de dueño+
     estado ya construido en services_archivos.validar_permiso_edicion
     (renombrado esta sesión, antes _validar_permiso_carga — mismo
     candado, reutilizado tal cual, no duplicado). FacturaLinea.save()
     ya recalcula difiere_de_oc automáticamente (Sub-fase 3.1b), sin
     necesitar ninguna lógica adicional aquí.
"""
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.sap_sync.models import PurchaseOrder

from . import services_archivos as sa
from .models import Factura, FacturaLinea, FacturaOrdenCompra
from .services import InvoicingService


def _pos_con_factura_activa():
    return FacturaOrdenCompra.objects.exclude(
        factura__estado='CANCELADO'
    ).values_list('purchase_order_id', flat=True)


def sede_de_oc(po):
    """
    Sede real de la OC — inferida del Appointment/Ticket FINALIZADO más
    reciente vinculado (mismo criterio que apps.base.oc_status.
    calcular_estado_despacho). Lanza ValidationError si, por alguna
    inconsistencia, la OC ya no tiene ningún Ticket FINALIZADO al momento
    de esta llamada (no debería ocurrir para una OC que ya pasó por
    listar_ocs_elegibles, pero se valida igual — defensa en profundidad
    contra una carrera entre el listado y la creación).
    """
    appointment = po.appointment_set.filter(
        ticket__estado='FINALIZADO'
    ).select_related('sede').order_by('-created_at').first()
    if appointment is None:
        raise ValidationError(f"La OC {po.doc_num} no tiene ningún Ticket finalizado.")
    return appointment.sede


@transaction.atomic
def lineas_disponibles_de_oc(purchase_order) -> list[dict]:
    """
    Líneas ACTIVAS de la OC con saldo_disponible > 0 (InvoicingService.
    saldo_disponible, Sub-fase 3.1). Una línea sin ninguna
    EntradaMercaderiaLinea (ValidationError) se omite en silencio: esa
    línea específica todavía no fue recibida físicamente — no es un error
    al armar el listado, es información legítima (una OC con Ticket
    FINALIZADO puede tener líneas que nunca llegaron a inspeccionarse).
    """
    disponibles = []
    for po_line in purchase_order.lines.filter(activa=True).order_by('line_num'):
        try:
            saldo = InvoicingService.saldo_disponible(po_line)
        except ValidationError:
            continue
        if saldo > 0:
            disponibles.append({'po_line': po_line, 'saldo': saldo})
    return disponibles


@transaction.atomic
def listar_ocs_elegibles(proveedor) -> list[dict]:
    """
    OCs elegibles para que `proveedor` (SupplierProfile) arme una Factura
    nueva. Devuelve una lista de dicts {'purchase_order', 'sede'} — no
    objetos PurchaseOrder sueltos — para que el template pueda mostrar la
    sede de cada OC sin una consulta adicional por fila.
    """
    candidatas = PurchaseOrder.objects.filter(
        card_code=proveedor.sap_card_code,
        appointment__ticket__estado='FINALIZADO',
    ).exclude(
        id__in=_pos_con_factura_activa()
    ).distinct().order_by('-created_at')

    elegibles = []
    for po in candidatas:
        if not lineas_disponibles_de_oc(po):
            continue
        try:
            sede = sede_de_oc(po)
        except ValidationError:
            continue
        elegibles.append({'purchase_order': po, 'sede': sede})
    return elegibles


def lineas_para_copiar(purchase_orders) -> list[dict]:
    """
    Grupos {'purchase_order', 'lineas': [...]} para la Pantalla de Copia
    — SIN tocar la BD (solo lecturas, ver docstring del módulo sobre por
    qué se agrupa aquí y no con `{% regroup %}`). `cantidad_oc` de
    referencia = po_line.quantity_sap (ver docstring de FacturaLinea,
    models.py — "sí puede copiarse de po_line.quantity_sap"); `precio_oc`
    no tiene ninguna fuente automática (idem) — se snapshotea igual al
    precio que el proveedor ingrese recién al crear (crear_factura_
    desde_ocs), no aquí.
    """
    grupos = []
    for po in purchase_orders:
        lineas = lineas_disponibles_de_oc(po)
        if not lineas:
            continue
        grupos.append({
            'purchase_order': po,
            'lineas': [
                {
                    'po_line': item['po_line'],
                    'saldo': item['saldo'],
                    'cantidad_oc': item['po_line'].quantity_sap,
                }
                for item in lineas
            ],
        })
    return grupos


@transaction.atomic
def _crear_factura_y_lineas(*, proveedor, purchase_order_ids, cabecera, lineas_payload):
    """Fase 1 de crear_factura_desde_ocs — ver docstring del módulo."""
    purchase_orders = list(PurchaseOrder.objects.filter(id__in=purchase_order_ids))
    if len(purchase_orders) != len(set(purchase_order_ids)):
        raise ValidationError("Una o más Órdenes de Compra seleccionadas ya no existen.")

    for po in purchase_orders:
        if po.card_code != proveedor.sap_card_code:
            raise ValidationError(f"La OC {po.doc_num} no pertenece a este proveedor.")

    sede_por_po = {po.id: sede_de_oc(po) for po in purchase_orders}
    if len({sede.id for sede in sede_por_po.values()}) > 1:
        raise ValidationError(
            "No se puede combinar Órdenes de Compra de sedes distintas en una misma Factura."
        )
    sede = next(iter(sede_por_po.values()))

    for po in purchase_orders:
        try:
            InvoicingService.validar_oc_disponible(po)
        except ValidationError as e:
            mensaje = '; '.join(e.messages) if hasattr(e, 'messages') else str(e)
            raise ValidationError(
                f"La OC {po.doc_num} ya no está disponible para facturar: {mensaje}"
            )

    factura = Factura.objects.create(proveedor=proveedor, sede=sede, estado='BORRADOR', **cabecera)

    for po in purchase_orders:
        FacturaOrdenCompra.objects.create(factura=factura, purchase_order=po)

    ids_ocs_seleccionadas = {po.id for po in purchase_orders}
    alguna_linea = False
    for fila in lineas_payload:
        cantidad = fila['cantidad']
        if cantidad <= 0:
            continue
        po_line = fila['po_line']
        if po_line.purchase_order_id not in ids_ocs_seleccionadas:
            raise ValidationError("Una línea no corresponde a ninguna de las OC seleccionadas.")

        saldo = InvoicingService.saldo_disponible(po_line, excluir_factura=factura)
        if cantidad > saldo:
            raise ValidationError(
                f"La línea {po_line.item_code} (OC {po_line.purchase_order.doc_num}) excede "
                f"el saldo disponible ({saldo})."
            )
        precio = fila['precio']
        FacturaLinea.objects.create(
            factura=factura, po_line=po_line,
            cantidad_oc=po_line.quantity_sap, precio_oc=precio,
            cantidad=cantidad, precio=precio,
            aplica_retencion=fila.get('aplica_retencion', False),
            aplica_detraccion=fila.get('aplica_detraccion', False),
        )
        alguna_linea = True

    if not alguna_linea:
        raise ValidationError(
            "Debe ingresar una cantidad mayor a 0 en al menos una línea para crear la Factura."
        )

    return factura


def _adjuntar_retencion_detraccion(factura, lineas_payload, usuario):
    """Fase 2 de crear_factura_desde_ocs — ver docstring del módulo."""
    lineas_por_po_line_id = {
        linea.po_line_id: linea for linea in factura.lineas.select_related('po_line').all()
    }
    for fila in lineas_payload:
        linea = lineas_por_po_line_id.get(fila['po_line'].id)
        if linea is None:
            continue  # línea con cantidad<=0 — nunca se creó en la fase 1

        archivo_ret = fila.get('archivo_retencion')
        archivo_det = fila.get('archivo_detraccion')
        if linea.aplica_retencion and archivo_ret:
            sa.cargar_archivo_factura_linea(linea, 'retencion', archivo_ret, usuario)
        if linea.aplica_detraccion and archivo_det:
            sa.cargar_archivo_factura_linea(linea, 'detraccion', archivo_det, usuario)

        # Candado ya construido (Sub-fase 3.1): si aplica_retencion/
        # aplica_detraccion quedó marcado pero, tras el intento de carga
        # de arriba, el campo sigue vacío (no se adjuntó ningún archivo),
        # rechaza — mismo mensaje ya usado en el resto del sistema, sin
        # duplicar esta lógica aquí.
        InvoicingService.validar_retencion_detraccion(linea)


def crear_factura_desde_ocs(*, proveedor, purchase_order_ids, cabecera, lineas_payload, usuario):
    """
    Punto 3 del pedido ("Copiar de OC(s)"): el primer guardado real en BD.
    Ver docstring del módulo para el detalle de las 2 fases.
    """
    factura = _crear_factura_y_lineas(
        proveedor=proveedor, purchase_order_ids=purchase_order_ids,
        cabecera=cabecera, lineas_payload=lineas_payload,
    )
    try:
        _adjuntar_retencion_detraccion(factura, lineas_payload, usuario)
    except ValidationError:
        factura.delete()
        raise
    return factura


def editar_cabecera_factura(factura, usuario, cabecera: dict):
    """
    Edita los campos de cabecera de una Factura mientras esté en
    BORRADOR/OBSERVADA — mismo candado que services_archivos.
    validar_permiso_edicion (dueño + estado), reutilizado tal cual.
    """
    sa.validar_permiso_edicion(factura, usuario)

    campos = [
        'doc_cur', 'tax_date', 'doc_due_date', 'num_at_card',
        'serie_comprobante', 'numero_comprobante', 'tipo_operacion',
        'clasificacion_bienes_servicios',
    ]
    for campo in campos:
        if campo in cabecera:
            setattr(factura, campo, cabecera[campo])
    factura.save(update_fields=campos + ['updated_at'])
    return factura


@transaction.atomic
def editar_linea_factura(linea, usuario, *, cantidad=None, precio=None,
                          aplica_retencion=None, aplica_detraccion=None):
    """
    Edita cantidad/precio/aplica_retencion/aplica_detraccion de una
    FacturaLinea mientras la Factura esté en BORRADOR/OBSERVADA —
    recalcula difiere_de_oc automáticamente (FacturaLinea.save(), Sub-
    fase 3.1b, sin cambios). `cantidad` no puede exceder saldo_disponible
    (excluyendo la propia Factura — mismo criterio ya usado en el resto
    del sistema para no contarse a sí misma dos veces).

    Actualiza SOLO los campos explícitamente provistos (None = "no
    tocar", permite un guardado parcial desde la UI — ej. solo tildar el
    checkbox de retención sin reenviar cantidad/precio).
    """
    factura = linea.factura
    sa.validar_permiso_edicion(factura, usuario)

    campos = []
    if cantidad is not None:
        saldo = InvoicingService.saldo_disponible(linea.po_line, excluir_factura=factura)
        if cantidad > saldo:
            raise ValidationError(
                f"No puede facturar {cantidad} — el saldo disponible es {saldo}."
            )
        linea.cantidad = cantidad
        campos.append('cantidad')
    if precio is not None:
        linea.precio = precio
        campos.append('precio')
    if aplica_retencion is not None:
        linea.aplica_retencion = aplica_retencion
        campos.append('aplica_retencion')
    if aplica_detraccion is not None:
        linea.aplica_detraccion = aplica_detraccion
        campos.append('aplica_detraccion')

    if not campos:
        return linea

    linea.save(update_fields=campos + ['difiere_de_oc'])
    return linea
