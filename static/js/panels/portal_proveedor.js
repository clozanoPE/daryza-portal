/**
 * Daryza VBS — portal_proveedor.js
 *
 * Lógica del portal del proveedor:
 *   - Selección de slot en la grilla semanal
 *   - Modal de agendar cita (OC selection)
 *   - Carga de COA por ítem
 *   - Generación de QR codes
 *
 * Depende de: modules/api.js, modules/ui.js, qrcode.min.js (CDN)
 */

/* ─── Selección de Slot ─────────────────────────────────────────────────── */
let _selectedSlotId   = null;
let _selectedSlotFecha = null;
let _selectedSlotHora  = null;

/**
 * Llamado al hacer clic en un slot disponible de la grilla.
 * @param {string} slotId
 * @param {string} fecha  YYYY-MM-DD
 * @param {string} hora   HH:MM
 */
function seleccionarSlot(slotId, fecha, hora) {
  // Quitar selección previa
  document.querySelectorAll('.slot-cell--selected').forEach(el => {
    el.classList.remove('slot-cell--selected');
  });

  // Marcar nuevo slot
  const clickedCell = document.querySelector(`[data-slot-id="${slotId}"]`);
  if (clickedCell) clickedCell.classList.add('slot-cell--selected');

  _selectedSlotId    = slotId;
  _selectedSlotFecha = fecha;
  _selectedSlotHora  = hora;

  // Actualizar resumen en el modal
  const elFecha = document.getElementById('modal-slot-fecha');
  const elHora  = document.getElementById('modal-slot-hora');
  if (elFecha) elFecha.textContent = fecha;
  if (elHora)  elHora.textContent  = hora;

  // Abrir modal de solicitud
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('modalAgendar'));
  modal.show();
}

/* ─── Envío del formulario de solicitud ─────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  // Generar QR para cada ticket confirmado
  document.querySelectorAll('.qr-code-item').forEach(el => {
    const token = el.dataset.token;
    if (token && typeof QRCode !== 'undefined') {
      new QRCode(el, {
        text: token, width: 110, height: 110,
        colorDark: '#1e1e2d', colorLight: '#ffffff',
        correctLevel: QRCode.CorrectLevel.H,
      });
    }
  });

  // Botón enviar del modal de agendar
  const btnEnviar = document.getElementById('btn-enviar-solicitud');
  if (btnEnviar) {
    btnEnviar.addEventListener('click', enviarSolicitud);
  }
});

async function enviarSolicitud() {
  const { showToast, btnLoading } = DzUI;
  const btn = document.getElementById('btn-enviar-solicitud');

  if (!_selectedSlotId) {
    showToast('Selecciona un horario disponible en la grilla.', 'warning');
    return;
  }

  const ocCheckboxes = document.querySelectorAll('.oc-checkbox:checked');
  if (!ocCheckboxes.length) {
    showToast('Debes seleccionar al menos una Orden de Compra.', 'warning');
    return;
  }

  const restoreBtn = btnLoading(btn, 'Enviando...');
  const formData = new FormData();
  formData.append('slot_id', _selectedSlotId);
  formData.append('lugar_entrega', document.getElementById('sel-sede')?.value || 'LURIN');
  ocCheckboxes.forEach(cb => formData.append('oc_ids', cb.value));

  const coaFile = document.getElementById('inp-coa-general')?.files[0];
  if (coaFile) formData.append('coa_pdf', coaFile);

  try {
    const data = await DzApi.postFormData('/appointments/api/solicitar-cita/', formData);
    if (data.status !== 'success') throw new Error(data.msg);

    showToast(data.msg || 'Solicitud enviada correctamente.', 'success');
    bootstrap.Modal.getInstance(document.getElementById('modalAgendar'))?.hide();
    setTimeout(() => window.location.reload(), 1200);
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    restoreBtn();
  }
}

/* ─── Carga de COA por ítem ─────────────────────────────────────────────── */

/**
 * Abre o cierra el panel de carga de COA para una cita confirmada.
 * @param {number} citaId
 */
async function abrirCargaDocumentos(citaId) {
  const panel      = document.getElementById(`document-panel-${citaId}`);
  const container = document.getElementById(`lineas-coa-${citaId}`);
  if (!panel || !container) return;

  const isOpen = !panel.classList.contains('d-none');
  panel.classList.toggle('d-none', isOpen);
  if (isOpen) return; // Si ya estaba abierto, solo cerramos

  DzUI.showSkeleton(container, 3);

  try {
    const data = await DzApi.getJSON(`/appointments/api/lineas-cita/${citaId}/`);
    if (data.status !== 'success') throw new Error(data.msg);
    container.innerHTML = buildCoaTable(data.lineas, citaId);
  } catch (err) {
    container.innerHTML = `<div class="alert alert-danger small py-2 mb-0"><i class="bi bi-x-circle me-1"></i>${err.message}</div>`;
  }
}

/**
 * Construye la tabla HTML de líneas que requieren COA, agrupadas por OC (Colapsable).
 * @param {Array} lineas
 * @param {number} citaId
 * @returns {string} HTML
 */
function buildCoaTable(lineas, citaId) {
  const lineaRequiere = lineas.filter(l => l.requiere_coa);
  if (!lineaRequiere.length) {
    return '<p class="text-muted text-xxs text-center py-3 mb-0"><i class="bi bi-check-circle text-success me-1"></i>No se requiere COA para esta cita.</p>';
  }

  // AJUSTE: Ahora agrupamos por el campo 'doc_num' que viene de tu nueva columna en la DB
  const grupos = lineaRequiere.reduce((acc, l) => {
    const oc = l.oc_num || 'Sin OC';
    if (!acc[oc]) acc[oc] = [];
    acc[oc].push(l);
    return acc;
  }, {});

  // Construcción del Acordeón colapsable
  let html = `<div class="accordion accordion-flush" id="acc-coa-${citaId}">`;

  Object.keys(grupos).forEach((oc, index) => {
    const items = grupos[oc];
    const collapseId = `coll-${citaId}-${index}`;

    html += `
      <div class="accordion-item border mb-2 rounded-2 overflow-hidden">
        <h2 class="accordion-header">
          <button class="accordion-button collapsed py-2 px-3 bg-light text-dark fw-bold" 
                  type="button" data-bs-toggle="collapse" data-bs-target="#${collapseId}" 
                  style="font-size: .7rem;">
            <i class="bi bi-cart3 me-2 text-success"></i> OC ${oc}
            <span class="badge bg-secondary ms-2" style="font-size: .55rem;">${items.length} ítems</span>
          </button>
        </h2>
        <div id="${collapseId}" class="accordion-collapse collapse" data-bs-parent="#acc-coa-${citaId}">
          <div class="accordion-body p-0">
            <table class="table table-sm align-middle mb-0" style="font-size:.72rem">
              <thead class="table-light">
                <tr>
                  <th class="ps-3 text-xxs">Ítem</th>
                  <th class="text-center text-xxs">Estado</th>
                  <th class="pe-3 text-xxs">Subir</th>
                </tr>
              </thead>
              <tbody>
                ${items.map(l => `
                  <tr id="row-coa-${l.po_line_id}">
                    <td class="ps-3">
                      <span class="fw-bold text-xxs d-block">${l.item_code}</span>
                      <small class="text-muted" style="font-size:.6rem">${l.description}</small>
                    </td>
                    <td id="coa-status-${l.po_line_id}" class="text-center">
                      ${l.coa_url
                        ? '<span class="badge bg-success-subtle text-success border border-success-subtle text-xxs">✓</span>'
                        : '<span class="badge bg-warning-subtle text-warning border border-warning-subtle text-xxs">!</span>'}
                    </td>
                    <td class="pe-3">
                      <div class="d-flex gap-1 align-items-center justify-content-end">
                        <input type="file" class="form-control form-control-sm" id="file-coa-${l.po_line_id}" accept=".pdf" style="font-size:.6rem;max-width:80px">
                        <button class="btn btn-success btn-sm py-0 px-2" style="font-size:.7rem"
                                onclick="subirCoaLinea(${l.po_line_id}, ${citaId}, this)">
                          <i class="bi bi-upload"></i>
                        </button>
                        ${l.coa_url ? `<a href="${l.coa_url}" target="_blank" class="btn btn-outline-success btn-sm py-0 px-2" style="font-size:.7rem"><i class="bi bi-eye"></i></a>` : ''}
                      </div>
                    </td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>
        </div>
      </div>`;
  });

  html += `</div><div id="coa-resumen-${citaId}" class="mt-2 small text-muted text-center"></div>`;
  return html;
}

/**
 * Sube el COA de una línea específica al servidor (y a OneDrive).
 * @param {number} lineaId
 * @param {number} citaId
 * @param {HTMLElement} btn
 */
async function subirCoaLinea(lineaId, citaId, btn) {
  const { showToast, btnLoading } = DzUI;
  const fileInput = document.getElementById(`file-coa-${lineaId}`);
  if (!fileInput?.files[0]) { showToast('Selecciona un archivo PDF.', 'warning'); return; }

  // --- FEEDBACK VISUAL INICIO ---
  // Deshabilitamos el input para evitar cambios durante la subida
  fileInput.disabled = true;
  // Cambiamos el botón a estado de carga con spinner
  //const restoreBtn = btnLoading(btn, '<span class="spinner-border spinner-border-sm" role="status"></span>');
  const restoreBtn = btnLoading(btn, '');
  // --
  //const restoreBtn = btnLoading(btn, '');
  const formData = new FormData();
  formData.append('coa_pdf', fileInput.files[0]);

  try {
    const data = await DzApi.postFormData(`/appointments/${citaId}/coa/${lineaId}/subir/`, formData);
    if (data.status !== 'success') throw new Error(data.msg);

    // Actualizar estado visual de la fila
    const statusCell = document.getElementById(`coa-status-${lineaId}`);
    if (statusCell) {
      statusCell.innerHTML = '<span class="badge bg-success-subtle text-success border border-success-subtle text-xxs">✓ Cargado</span>';
    }
    fileInput.value = '';
    showToast('COA cargado correctamente.', 'success');

    // Verificar si todos los COA están completos
    verificarCompletitudCoa(citaId);
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    restoreBtn();
  }
}

/**
 * Verifica si todos los COA de una cita están completos y muestra un mensaje.
 * @param {number} citaId
 */
function verificarCompletitudCoa(citaId) {
  const pendientes = document.querySelectorAll(`#lineas-coa-${citaId} .badge-warning-subtle, #lineas-coa-${citaId} .text-warning`).length;
  const resumen = document.getElementById(`coa-resumen-${citaId}`);
  if (!resumen) return;

  if (pendientes === 0) {
    resumen.innerHTML = '<span class="text-success fw-bold"><i class="bi bi-check-circle-fill me-1"></i>¡Todos los COA están cargados! Puede presentarse a Vigilancia.</span>';
  } else {
    resumen.innerHTML = `<span class="text-warning"><i class="bi bi-exclamation-triangle me-1"></i>Faltan ${pendientes} COA por cargar. Sin ellos no podrá ingresar a planta.</span>`;
  }
}

/* ─── Datos de Ingreso: vehículo/conductor (sesión 13) ──────────────────── */

/**
 * Abre o cierra el panel del formulario de Datos de Ingreso para una cita.
 * @param {number} citaId
 */
function toggleDatosIngreso(citaId) {
  const panel = document.getElementById(`datos-ingreso-panel-${citaId}`);
  if (!panel) return;
  panel.classList.toggle('d-none');
}

/**
 * Envía el formulario de Datos de Ingreso (placa/DNI/nombre del conductor).
 * Informativo para Vigilancia, no bloqueante: se acepta guardar aunque
 * falte algún campo, siempre que al menos uno tenga contenido (el backend
 * también lo valida).
 *
 * @param {Event} event
 * @param {number} citaId
 * @returns {boolean} false, para evitar el submit nativo del <form>
 */
async function guardarDatosIngreso(event, citaId) {
  event.preventDefault();
  const { showToast, btnLoading } = DzUI;
  const form = event.target;
  const btn = form.querySelector('button[type="submit"]');
  const restoreBtn = btn ? btnLoading(btn, 'Guardando...') : () => {};

  try {
    const formData = new FormData(form);
    const data = await DzApi.postFormData(`/appointments/${citaId}/datos-ingreso/`, formData);
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Datos guardados.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    restoreBtn();
  }

  return false;
}