/**
 * Daryza VBS — calendar_handler.js
 *
 * Gestión del calendario de slots en el panel de horarios (Almacén/Admin).
 * Las interacciones del proveedor están en: panels/portal_proveedor.js
 *
 * Depende de: modules/api.js, modules/ui.js
 */

document.addEventListener('DOMContentLoaded', () => {
  // Si existe el calendario de administración, inicializarlo
  const calContainer = document.getElementById('calendar-admin');
  if (!calContainer) return;

  // La lógica del calendario de admin se inicializa aquí
  // (este archivo se mantiene para compatibilidad con panel_horarios.html)
  console.debug('[DZ] calendar_handler.js inicializado');
});
