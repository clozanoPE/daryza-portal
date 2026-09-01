/**
 * Daryza VBS — entrada_mercaderia.js  (sesión 99)
 *
 * JS de la pantalla de detalle de la Entrada de Mercadería (GRPO):
 *   - Guardar cantidad / lote / fechas de una línea (AJAX JSON, al perder
 *     el foco de cada campo)
 *   - Enviar la Entrada a SAP B1 (valida client-side que todas las líneas
 *     tengan lote — el backend valida exactamente lo mismo en
 *     services_entrada.enviar_a_sap)
 *
 * Depende de: modules/api.js (DzApi), modules/ui.js (DzUI).
 */

async function guardarLineaEntrada(lineaId) {
  const { showToast } = DzUI;
  const fila = document.querySelector(`tr[data-linea-id="${lineaId}"]`);
  if (!fila) return;

  const body = {
    cantidad: fila.querySelector('.inp-cantidad')?.value,
    numero_lote: fila.querySelector('.inp-lote')?.value ?? '',
    fecha_vencimiento_lote: fila.querySelector('.inp-venc')?.value ?? '',
    fecha_fabricacion_lote: fila.querySelector('.inp-fab')?.value ?? '',
  };

  try {
    const data = await DzApi.postJSON(
      `/operations/api/entrada-mercaderia/linea/${lineaId}/editar/`, body,
    );
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Línea actualizada.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function reabrirEntrada(entradaId) {
  const { showToast } = DzUI;
  try {
    const data = await DzApi.postJSON(
      `/operations/api/entrada-mercaderia/${entradaId}/reabrir/`, {},
    );
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Entrada reabierta.', 'success');
    setTimeout(() => window.location.reload(), 800);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function enviarEntradaASap(entradaId) {
  const { showToast, btnLoading } = DzUI;
  const btn = document.getElementById('btn-enviar-entrada');

  // Validación client-side: solo las líneas gestionadas por lote tienen
  // un `.inp-lote` (las demás muestran un campo deshabilitado sin esa
  // clase), así que este selector ya cubre exactamente las obligatorias.
  const sinLote = [...document.querySelectorAll('.inp-lote')]
    .filter(inp => !inp.value.trim());
  if (sinLote.length) {
    showToast('Complete el número de lote de las líneas gestionadas por lote antes de enviar a SAP B1.', 'warning');
    sinLote[0].focus();
    return;
  }

  const restoreBtn = btn ? btnLoading(btn, 'Enviando...') : () => {};
  try {
    const data = await DzApi.postJSON(
      `/operations/api/entrada-mercaderia/${entradaId}/enviar-a-sap/`, {},
    );
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Entrada de Mercadería enviada a SAP B1.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
    restoreBtn();
  }
}
