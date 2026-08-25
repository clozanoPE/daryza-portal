/**
 * Daryza VBS — nueva_factura.js
 *
 * JS de la Pantalla de Copia (Sub-fase 3.4, "Copiar de OC(s)"):
 *   - Muestra/oculta el input de archivo de retención/detracción por línea
 *   - Envía la creación de la Factura (POST multipart, con los archivos
 *     de retención/detracción ya adjuntos, si aplica)
 *
 * Depende de: modules/api.js, modules/ui.js
 */

/**
 * Muestra u oculta el panel de "documento requerido" de una línea al
 * marcar/desmarcar aplica_retencion/aplica_detraccion — y marca el input
 * de archivo como required mientras esté visible, para que el navegador
 * ya bloquee un submit sin archivo antes de llegar a crearFactura().
 * @param {HTMLInputElement} checkbox
 * @param {'retencion'|'detraccion'} tipo
 */
function toggleDocRequerido(checkbox, tipo) {
  const panel = document.getElementById(`doc-${tipo}-${checkbox.dataset.poLine}`);
  if (!panel) return;
  panel.classList.toggle('d-none', !checkbox.checked);
  const fileInput = panel.querySelector('input[type="file"]');
  if (fileInput) fileInput.required = checkbox.checked;
}

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('btn-crear-factura');
  if (btn) btn.addEventListener('click', crearFactura);
});

async function crearFactura() {
  const { showToast, btnLoading } = DzUI;
  const form = document.getElementById('form-crear-factura');
  const btn = document.getElementById('btn-crear-factura');
  if (!form || !btn) return;

  // Validación client-side (punto 2 del pedido): no permite continuar
  // sin el documento de retención/detracción si el checkbox está
  // marcado — el backend valida exactamente lo mismo (InvoicingService.
  // validar_retencion_detraccion), esto solo evita el viaje redondo.
  const checkboxesMarcados = form.querySelectorAll('.chk-retencion:checked, .chk-detraccion:checked');
  for (const cb of checkboxesMarcados) {
    const tipo = cb.classList.contains('chk-retencion') ? 'retencion' : 'detraccion';
    const panel = document.getElementById(`doc-${tipo}-${cb.dataset.poLine}`);
    const fileInput = panel ? panel.querySelector('input[type="file"]') : null;
    if (!fileInput || !fileInput.files.length) {
      showToast(`Debe adjuntar el documento de ${tipo} para la línea marcada.`, 'warning');
      return;
    }
  }

  const restoreBtn = btnLoading(btn, 'Creando...');
  try {
    const formData = new FormData(form);
    const data = await DzApi.postFormData('/invoicing/nueva/crear/', formData);
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Factura creada correctamente.', 'success');
    setTimeout(() => { window.location.href = `/invoicing/factura/${data.factura_id}/`; }, 800);
  } catch (err) {
    showToast(err.message, 'error');
    restoreBtn();
  }
}
