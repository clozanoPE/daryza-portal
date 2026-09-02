/**
 * Daryza VBS — mis_facturas.js
 *
 * Único uso hoy: eliminar un borrador de Factura atascado desde el
 * listado del proveedor (sesión 101, Obs 3). Misma acción que el botón
 * de factura_detalle.html — se duplica la función acá (mínima) en vez de
 * cargar todo factura_detalle.js (que trae handlers de DOMContentLoaded
 * para elementos que no existen en este listado).
 *
 * Depende de: modules/api.js, modules/ui.js (cargados en base.html).
 */
async function eliminarBorradorFactura(facturaId) {
  const { showToast, confirmDialog } = DzUI;
  const ok = await confirmDialog(
    '¿Eliminar este borrador de Factura?',
    'Esta acción no se puede deshacer. Las Órdenes de Compra quedarán disponibles para volver a copiarlas. Los archivos ya subidos no se recuperan.',
    'warning',
  );
  if (!ok) return;

  try {
    const data = await DzApi.postJSON(`/invoicing/factura/${facturaId}/eliminar/`, {});
    if (data.status !== 'success') throw new Error(data.msg);
    showToast(data.msg || 'Borrador eliminado.', 'success');
    setTimeout(() => window.location.reload(), 900);
  } catch (err) {
    showToast(err.message, 'error');
  }
}
