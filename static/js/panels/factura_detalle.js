/**
 * Daryza VBS — factura_detalle.js
 *
 * JS del detalle de una Factura (Sub-fase 3.4/3.2):
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

async function subirArchivoFactura(tipo, facturaId) {
  const { showToast } = DzUI;
  const fileInput = document.getElementById(`file-${tipo}`);
  if (!fileInput || !fileInput.files.length) { showToast('Selecciona un archivo.', 'warning'); return; }

  const formData = new FormData();
  formData.append('archivo', fileInput.files[0]);
  try {
    const data = await DzApi.postFormData(`/invoicing/factura/${facturaId}/archivo/${tipo}/`, formData);
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Archivo cargado.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function guardarLinea(lineaId) {
  const { showToast } = DzUI;
  const cantidadInput = document.querySelector(`.inp-cantidad[data-linea="${lineaId}"]`);
  const precioInput = document.querySelector(`.inp-precio[data-linea="${lineaId}"]`);
  try {
    const data = await DzApi.postJSON(`/invoicing/factura-linea/${lineaId}/editar/`, {
      cantidad: cantidadInput ? cantidadInput.value : undefined,
      precio: precioInput ? precioInput.value : undefined,
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

async function subirDocLinea(lineaId, tipo) {
  const { showToast } = DzUI;
  const fileInput = document.getElementById(`file-${tipo}-${lineaId}`);
  if (!fileInput || !fileInput.files.length) { showToast('Selecciona un archivo.', 'warning'); return; }

  const formData = new FormData();
  formData.append('archivo', fileInput.files[0]);
  try {
    const data = await DzApi.postFormData(`/invoicing/factura-linea/${lineaId}/archivo/${tipo}/`, formData);
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Documento cargado.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

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
