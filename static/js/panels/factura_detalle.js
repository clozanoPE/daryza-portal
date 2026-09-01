/**
 * Daryza VBS — factura_detalle.js
 *
 * JS del detalle de una Factura (Sub-fase 3.4/3.2/3.5):
 *   - Guardar cabecera (AJAX JSON)
 *   - Subir archivos de Factura (XML/PDF/CDR) — endpoint ya existente
 *     de la Sub-fase 3.2
 *   - Guardar línea (cantidad/precio) y togglear aplica_retencion/
 *     aplica_detraccion (AJAX JSON)
 *   - Subir documento de retención/detracción por línea — mismo
 *     endpoint ya existente de la Sub-fase 3.2, ahora alcanzable porque
 *     la FacturaLinea ya existe (a diferencia de la Pantalla de Copia,
 *     antes de crear la Factura)
 *   - Enviar a Revisión
 *   - Aprobar / Observar (Sub-fase 3.5, solo Compras — los botones y el
 *     modal correspondiente solo se renderizan en el template cuando
 *     es_compras y puede_actuar_compras son verdaderos, pero estas
 *     funciones no necesitan saberlo: si no hay nada que llamarlas,
 *     nunca se invocan)
 *
 * Depende de: modules/api.js, modules/ui.js
 */

async function guardarCabecera(facturaId) {
  const { showToast, btnLoading } = DzUI;
  const form = document.getElementById('form-cabecera');
  if (!form) return;
  const btn = form.querySelector('button[type="button"]');
  const restoreBtn = btn ? btnLoading(btn, 'Guardando...') : () => {};

  const body = Object.fromEntries(new FormData(form).entries());
  try {
    const data = await DzApi.postJSON(`/invoicing/factura/${facturaId}/editar/`, body);
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Cabecera actualizada.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    restoreBtn();
  }
}

async function subirArchivoFactura(tipo, facturaId, btn) {
  const { showToast, btnLoading } = DzUI;
  const fileInput = document.getElementById(`file-${tipo}`);
  if (!fileInput || !fileInput.files.length) { showToast('Selecciona un archivo.', 'warning'); return; }

  // Sesión 99c: feedback visible mientras sube (la subida real a OneDrive
  // tarda 1-3s; sin esto el botón parece congelado). El spinner se deja
  // puesto en el camino de éxito — la página recarga a continuación.
  const restoreBtn = btn ? btnLoading(btn, 'Cargando…') : () => {};
  const formData = new FormData();
  formData.append('archivo', fileInput.files[0]);
  try {
    const data = await DzApi.postFormData(`/invoicing/factura/${facturaId}/archivo/${tipo}/`, formData);
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Archivo cargado.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
    restoreBtn();
  }
}

async function guardarLinea(lineaId) {
  // Sesión 92: precio ya no es editable — solo cantidad. El endpoint
  // ahora rechaza explícitamente cualquier payload que incluya 'precio'.
  const { showToast } = DzUI;
  const cantidadInput = document.querySelector(`.inp-cantidad[data-linea="${lineaId}"]`);
  try {
    const data = await DzApi.postJSON(`/invoicing/factura-linea/${lineaId}/editar/`, {
      cantidad: cantidadInput ? cantidadInput.value : undefined,
    });
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Línea actualizada.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/**
 * @param {HTMLInputElement} checkbox
 * @param {'retencion'|'detraccion'} tipo
 */
async function toggleFlagLinea(checkbox, tipo) {
  const { showToast } = DzUI;
  const lineaId = checkbox.dataset.linea;
  const body = tipo === 'retencion'
    ? { aplica_retencion: checkbox.checked }
    : { aplica_detraccion: checkbox.checked };
  try {
    const data = await DzApi.postJSON(`/invoicing/factura-linea/${lineaId}/editar/`, body);
    if (data.status !== 'success') throw new Error(data.msg);
    window.location.reload();
  } catch (err) {
    showToast(err.message, 'error');
    checkbox.checked = !checkbox.checked;
  }
}

async function subirDocLinea(lineaId, tipo, btn) {
  const { showToast, btnLoading } = DzUI;
  const fileInput = document.getElementById(`file-${tipo}-${lineaId}`);
  if (!fileInput || !fileInput.files.length) { showToast('Selecciona un archivo.', 'warning'); return; }

  const restoreBtn = btn ? btnLoading(btn, 'Cargando…') : () => {};
  const formData = new FormData();
  formData.append('archivo', fileInput.files[0]);
  try {
    const data = await DzApi.postFormData(`/invoicing/factura-linea/${lineaId}/archivo/${tipo}/`, formData);
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Documento cargado.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
    restoreBtn();
  }
}

/**
 * Sesión 99c — "N° Doc. Proveedor" (num_at_card) se arma solo con Serie +
 * Número: SERIE-NNNNNNNN (correlativo con ceros a la izquierda hasta 8
 * dígitos). Input de solo lectura. Mismo criterio que la Pantalla de Copia.
 */
function syncNumAtCard() {
  const serie = (document.getElementById('det-serie')?.value || '').trim().toUpperCase();
  const numeroRaw = (document.getElementById('det-numero')?.value || '').trim();
  const dest = document.getElementById('det-num-at-card');
  if (!dest) return;
  if (!serie && !numeroRaw) { dest.value = ''; return; }
  const numero = /^\d+$/.test(numeroRaw) ? numeroRaw.padStart(8, '0') : numeroRaw;
  dest.value = serie && numero ? `${serie}-${numero}` : (serie || numero);
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.js-serie-num').forEach(inp => {
    inp.addEventListener('input', syncNumAtCard);
  });
});

async function enviarARevision(facturaId) {
  const { showToast, confirmDialog } = DzUI;
  const ok = await confirmDialog(
    '¿Enviar a revisión?',
    'No podrá editar la Factura ni sus archivos hasta que Compras responda.',
    'question',
  );
  if (!ok) return;

  try {
    const data = await DzApi.postJSON(`/invoicing/factura/${facturaId}/enviar-a-revision/`, {});
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Factura enviada a revisión.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/**
 * Sub-fase 3.5 — Compras aprueba la Factura. InvoicingService.
 * aprobar_factura ya re-valida firma/CDR/importe por su cuenta (defensa
 * en profundidad) — si rechaza, el mensaje real del servicio se muestra
 * tal cual, sin adornarlo.
 */
async function aprobarFactura(facturaId) {
  const { showToast, confirmDialog } = DzUI;
  const ok = await confirmDialog(
    '¿Aprobar esta Factura?',
    'Quedará lista para sincronizarse con SAP.',
    'question',
  );
  if (!ok) return;

  try {
    const data = await DzApi.postJSON(`/invoicing/compras/factura/${facturaId}/aprobar/`, {});
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Factura aprobada.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/**
 * Sub-fase 3.5 — Compras observa la Factura. El texto no vacío es
 * obligatorio (mismo criterio que el motivo de rechazo de citas) — se
 * valida aquí como conveniencia de UX, pero el candado real vive en
 * InvoicingService.observar_factura (defensa en profundidad).
 */
async function confirmarObservarFactura(facturaId) {
  const { showToast } = DzUI;
  const textarea = document.getElementById('txtObservacionFactura');
  const texto = textarea ? textarea.value.trim() : '';
  if (!texto) { showToast('Debe ingresar un texto de observación.', 'warning'); return; }

  try {
    const data = await DzApi.postJSON(`/invoicing/compras/factura/${facturaId}/observar/`, { texto });
    if (data.status !== 'success') throw new Error(data.msg);
    let msg = data.msg || 'Factura observada.';
    if (data.email_error) msg += ' (Aviso: no se pudo notificar por correo al proveedor.)';
    showToast(msg, 'success');
    const modalEl = document.getElementById('modalObservarFactura');
    if (modalEl && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
    setTimeout(() => window.location.reload(), 1200);
  } catch (err) {
    showToast(err.message, 'error');
  }
}
