/**
 * Daryza VBS — ticket_actions.js
 *
 * Acciones operativas del Ticket: autorizar ingreso, autorizar almacén,
 * registrar inspección (Calidad/Almacén), registrar salida.
 *
 * Depende de: modules/api.js, modules/ui.js (cargados antes en base.html)
 */

/**
 * Mapa de acción → URL del backend.
 * Centralizado aquí para evitar hardcoding en templates.
 */
const ACTION_URLS = {
  'autorizar-ingreso':   '/operations/api/autorizar-ingreso/',
  'autorizar-almacen':   '/operations/api/autorizar-almacen/',
  'registrar-salida':    '/operations/api/registrar-salida/',
  // Vigilancia legacy (mismo endpoint que autorizar-ingreso)
  'confirmar-llegada':   '/operations/api/autorizar-ingreso/',
  'confirmar-salida':    '/operations/api/registrar-salida/',
};

/**
 * Ejecuta una acción de ticket con confirmación previa.
 * Usado en los botones de detalle_ticket y panel_vigilancia.
 *
 * @param {string} accion  - Clave de ACTION_URLS
 * @param {number} ticketId
 * @param {string} mensaje - Texto del confirm dialog
 * @param {string} [_csrf] - Ignorado (se toma automáticamente)
 */
async function accionTicket(accion, ticketId, mensaje, _csrf) {
  const { confirmDialog, showToast } = DzUI;
  const { postJSON } = DzApi;

  const confirmed = await confirmDialog('Confirmar acción', mensaje, 'question');
  if (!confirmed) return;

  const url = ACTION_URLS[accion];
  if (!url) { showToast(`Acción desconocida: ${accion}`, 'error'); return; }

  try {
    const data = await postJSON(url, { ticket_id: ticketId });
    showToast(data.msg || 'Operación completada.', 'success');

    // Recarga la página tras un breve delay para reflejar el nuevo estado
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/**
 * "Iniciar Recepción" (ALMACEN o MATERIA_PRIMA, Fase 5 — sesión 31).
 *
 * Lee muelle (#input-muelle) y el checkbox "Solicitar Inspección Calidad"
 * (#chk-requiere-calidad) del formulario en la tarjeta de acción, y los
 * envía junto con confirmado al mismo endpoint que ya usaba el botón
 * simple de Almacén (/operations/api/autorizar-almacen/, reescrito en el
 * backend en la Fase 3+4 para aceptar ambos actores).
 *
 * Para Materia Prima el backend EXIGE confirmado:true o rechaza con 400
 * (OperationsService.autorizar_almacen) — por eso se muestra un diálogo de
 * confirmación explícito adicional antes de enviar; si el usuario cancela,
 * no se envía ninguna petición. Almacén no necesita este paso extra.
 *
 * @param {number} ticketId      - Ticket.pk real, usado solo para la llamada al backend.
 * @param {number} appointmentId - Número que ve el usuario ("Ticket #X", sesión 56:
 *                                 Ticket.id y Appointment.id son secuencias
 *                                 independientes que solo coinciden por casualidad).
 * @param {boolean} esMateriaPrima - true si el actor con el turno es MATERIA_PRIMA
 */
async function iniciarRecepcion(ticketId, appointmentId, esMateriaPrima) {
  const { confirmDialog, showToast, btnLoading } = DzUI;
  const { postJSON } = DzApi;

  const muelle = document.getElementById('input-muelle')?.value?.trim() || '';
  const requiereCalidad = document.getElementById('chk-requiere-calidad')?.checked ?? false;

  if (esMateriaPrima) {
    const confirmado = await confirmDialog(
      'Confirmar Iniciar Recepción — Materia Prima',
      `Vas a iniciar la recepción como Materia Prima para el Ticket #${appointmentId}` +
        (requiereCalidad ? ', con Inspección de Calidad.' : ', SIN Inspección de Calidad.') +
        ' Esta acción requiere tu confirmación explícita antes de continuar. ¿Confirmas?',
      'warning'
    );
    if (!confirmado) return;
  } else {
    const confirmado = await confirmDialog(
      'Confirmar acción',
      `¿Iniciar recepción en Almacén para Ticket #${appointmentId}?`,
      'question'
    );
    if (!confirmado) return;
  }

  const btn = document.getElementById('btn-iniciar-recepcion');
  const restoreBtn = btn ? btnLoading(btn, 'Iniciando...') : () => {};

  try {
    const data = await postJSON('/operations/api/autorizar-almacen/', {
      ticket_id: ticketId,
      muelle,
      requiere_calidad: requiereCalidad,
      confirmado: esMateriaPrima,
    });
    showToast(data.msg || 'Recepción iniciada.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    restoreBtn();
  }
}

/* ─── Panel de Inspección (Almacén / Calidad) ────────────────────────────── */

/**
 * Recopila los datos del formulario de inspección y los envía al backend.
 * El formulario de inspección usa data-inspeccion-id y campos .insp-estado,
 * .insp-cantidad, .insp-comentario dentro de cada fila.
 *
 * La búsqueda de filas se limita al contenedor #mi-sesion-panel (la pestaña
 * "Mi Sesión", editable) para no arrastrar filas de la pestaña "Estado Actual"
 * (solo lectura, que también puede tener data-inspeccion-id de otras etapas
 * si en el futuro se reutiliza esta clase para otra tabla editable).
 *
 * @param {number} ticketId
 * @param {string} area  - 'calidad' | 'almacen'
 */
async function guardarInspeccion(ticketId, area) {
  const { confirmDialog, showToast, btnLoading } = DzUI;
  const { postJSON } = DzApi;

  const btn = document.getElementById(`btn-guardar-inspeccion-${area}`);
  if (!btn) return;

  const confirmed = await confirmDialog(
    'Finalizar Inspección',
    `¿Deseas guardar la inspección de ${area.toUpperCase()} y avanzar al siguiente paso?`,
    'question'
  );
  if (!confirmed) return;

  const restoreBtn = btnLoading(btn, 'Guardando...');

  try {
    const sesionPanel = document.getElementById('mi-sesion-panel');
    const filas = (sesionPanel || document).querySelectorAll(`[data-inspeccion-id]`);
    const resultados = Array.from(filas).map(fila => {
      const id = fila.dataset.inspeccionId;
      return {
        inspeccion_id:       parseInt(id),
        estado:              fila.querySelector('.insp-estado')?.value    || 'EN_PROCESO',
        cantidad_modificada: fila.querySelector('.insp-cantidad')?.value  || '0',
        comentario:          fila.querySelector('.insp-comentario')?.value || '',
      };
    }).filter(r => r.inspeccion_id);

    const data = await postJSON('/operations/api/registrar-inspeccion/', {
      ticket_id: ticketId,
      resultados,
    });
    showToast(data.msg || 'Inspección guardada.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    restoreBtn();
  }
}

/* ─── Panel Compras ───────────────────────────────────────────────────────── */

/** Renderiza la tabla de líneas de OC con checkboxes de COA. */
function renderTablaItems(lineas, citaId) {
  if (!lineas || !lineas.length) {
    return '<p class="text-muted small text-center py-3">Sin líneas de OC.</p>';
  }

  const rows = lineas.map(l => `
    <tr>
      <td class="text-xxs fw-bold">${l.doc_num}</td>
      <td class="text-xxs">
        <span class="badge ${l.es_mp ? 'bg-info-subtle text-info border border-info-subtle' : 'bg-light text-muted border'}" style="font-size:.6rem">
          ${l.es_mp ? '<i class="bi bi-flask me-1"></i>MP' : 'COM'}
        </span>
      </td>
      <td class="text-xxs"><span class="fw-bold">${l.item_code}</span><br><small class="text-muted">${l.description}</small></td>
      <td class="text-xs text-end">${l.quantity_sap} ${l.und_medida}</td>
      <td class="text-center">
        <div class="form-check form-switch d-flex justify-content-center m-0">
          <input class="form-check-input coa-toggle" type="checkbox" 
                 data-po-line-id="${l.po_line_id}"
                 id="coa-${citaId}-${l.po_line_id}"
                 ${l.requiere_coa ? 'checked' : ''}>
        </div>
      </td>
    </tr>`).join('');

  return `
    <table class="table table-sm table-hover align-middle mb-0">
      <thead>
        <tr>
          <th class="text-xxs text-muted fw-bold">OC</th>
          <th class="text-xxs text-muted fw-bold">Tipo</th>
          <th class="text-xxs text-muted fw-bold">Ítem</th>
          <th class="text-xxs text-muted fw-bold text-end">Cant.</th>
          <th class="text-xxs text-muted fw-bold text-center">Req. COA</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

/** Carga las líneas de OC para el panel de configuración de Compras. */
async function cargarLineas(citaId) {
  const container = document.getElementById(`lineas-container-${citaId}`);
  if (!container) return;

  DzUI.showSkeleton(container, 4);
  try {
    const data = await DzApi.getJSON(`/operations/api/compras/lineas/${citaId}/`);
    if (data.status !== 'success') throw new Error(data.msg);
    container.innerHTML = renderTablaItems(data.lineas, citaId);
  } catch (err) {
    container.innerHTML = `<div class="alert alert-danger small py-2">${err.message}</div>`;
  }
}

/** Guarda la configuración de COA editada por Compras. */
async function guardarConfiguracion(citaId) {
  const { showToast, btnLoading } = DzUI;
  const btn = document.querySelector(`#config-panel-${citaId} .btn-guardar-config`);
  const restoreBtn = btn ? btnLoading(btn, 'Guardando...') : () => {};

  try {
    const checkboxes = document.querySelectorAll(`#lineas-container-${citaId} .coa-toggle`);
    const lineas = Array.from(checkboxes).map(cb => ({
      po_line_id:  parseInt(cb.dataset.poLineId),
      requiere_coa: cb.checked,
    }));

    const data = await DzApi.postJSON('/operations/api/configurar-items-coa/', {
      appointment_id: citaId,
      lineas,
    });
    showToast(data.msg || 'Configuración guardada.', 'success');
    document.getElementById(`config-panel-${citaId}`)?.classList.add('d-none');
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    restoreBtn();
  }
}

/** Prepara la acción de confirmar/rechazar cita, con modal de observaciones. */
let _pendingActionCompras = null;

function prepararAccionCompras(tipo, citaId) {
  _pendingActionCompras = { tipo, citaId };
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('modalObservaciones'));
  document.getElementById('modalObsTitle').textContent = tipo === 'confirmar' ? 'Confirmar Cita' : 'Rechazar Cita';
  document.getElementById('txtObservaciones').value = '';

  // Motivo de rechazo: solo visible/obligatorio cuando tipo='rechazar'.
  const grupoMotivo = document.getElementById('grupoMotivoRechazo');
  const selMotivo   = document.getElementById('selMotivoRechazo');
  if (grupoMotivo) grupoMotivo.classList.toggle('d-none', tipo !== 'rechazar');
  if (selMotivo)   selMotivo.value = '';

  modal.show();
}

document.addEventListener('DOMContentLoaded', () => {
  const btnConfirmar = document.getElementById('btnConfirmarAccionCompras');
  if (!btnConfirmar) return;

  btnConfirmar.addEventListener('click', async () => {
    if (!_pendingActionCompras) return;
    const { tipo, citaId } = _pendingActionCompras;
    const observaciones = document.getElementById('txtObservaciones')?.value?.trim() || '';

    const payload = { appointment_id: citaId, observaciones };
    if (tipo === 'rechazar') {
      const motivoRechazo = document.getElementById('selMotivoRechazo')?.value || '';
      if (!motivoRechazo) {
        DzUI.showToast('Selecciona un motivo de rechazo.', 'warning');
        return; // el backend también lo exige, pero no vale la pena ni el viaje
      }
      payload.motivo_rechazo = motivoRechazo;
    }

    const restoreBtn = DzUI.btnLoading(btnConfirmar, 'Procesando...');

    // ACCION_CITA_API_PREFIX: inyectado por cada template que carga este
    // script ('/operations/api/compras/' en panel_compras.html,
    // '/operations/api/' en panel_almacen.html) — antes esta URL estaba
    // hardcodeada al prefijo de Compras sin importar qué panel llamara a
    // esta función, así que una cuenta ALMACEN-only que confirmaba o
    // rechazaba desde panel_almacen.html terminaba golpeando un endpoint
    // @compras_required y era redirigida a login en vez de completar la
    // acción (bug encontrado al implementar motivo_rechazo obligatorio:
    // sin este fix, la validación nueva nunca se habría ejercitado en el
    // camino real de Almacén).
    const prefix = typeof ACCION_CITA_API_PREFIX !== 'undefined'
      ? ACCION_CITA_API_PREFIX
      : '/operations/api/compras/';
    const url = tipo === 'confirmar' ? `${prefix}confirmar-cita/` : `${prefix}rechazar-cita/`;

    try {
      const data = await DzApi.postJSON(url, payload);
      bootstrap.Modal.getInstance(document.getElementById('modalObservaciones'))?.hide();
      DzUI.showToast(data.msg || 'Acción completada.', tipo === 'confirmar' ? 'success' : 'warning');
      setTimeout(() => window.location.reload(), 1000);
    } catch (err) {
      DzUI.showToast(err.message, 'error');
    } finally {
      restoreBtn();
    }
    _pendingActionCompras = null;
  });
});

// Listener para rotar iconos al expandir/colapsar OCs
document.querySelectorAll('.collapse').forEach(collapseEl => {
  collapseEl.addEventListener('show.bs.collapse', () => {
    const header = document.querySelector(`[data-bs-target="#${collapseEl.id}"]`);
    header?.querySelector('.bi-chevron-right')?.style.setProperty('transform', 'rotate(90deg)');
  });

  collapseEl.addEventListener('hide.bs.collapse', () => {
    const header = document.querySelector(`[data-bs-target="#${collapseEl.id}"]`);
    header?.querySelector('.bi-chevron-right')?.style.setProperty('transform', 'rotate(0deg)');
  });
});