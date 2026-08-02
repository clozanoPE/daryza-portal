/**
 * Daryza VBS — Módulo UI
 * @module ui
 *
 * Funciones de interfaz reutilizables:
 *   - Toast / Notificaciones
 *   - Skeleton loaders
 *   - Confirm dialogs (SweetAlert2)
 *   - Helpers de DOM
 */

/**
 * Muestra una notificación toast breve.
 * @param {string} message
 * @param {'success'|'error'|'warning'|'info'} type
 * @param {number} duration  ms antes de desaparecer
 */
const showToast = (message, type = 'success', duration = 3500) => {
  const icons = { success: 'bi-check-circle-fill', error: 'bi-x-circle-fill', warning: 'bi-exclamation-triangle-fill', info: 'bi-info-circle-fill' };
  const colors = { success: '#29b44a', error: '#dc3545', warning: '#fd7e14', info: '#0d6efd' };

  // Remove existing toast
  document.querySelectorAll('.dz-toast').forEach(t => t.remove());

  const toast = document.createElement('div');
  toast.className = 'dz-toast animate-fadein';
  toast.style.borderLeft = `4px solid ${colors[type] || colors.info}`;
  toast.innerHTML = `<i class="bi ${icons[type] || icons.info}" style="color:${colors[type]};font-size:1.1rem;flex-shrink:0"></i><span>${message}</span>`;
  document.body.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('hide');
    setTimeout(() => toast.remove(), 350);
  }, duration);
};

/**
 * Confirm dialog con SweetAlert2.
 * @param {string} title
 * @param {string} text
 * @param {'question'|'warning'} icon
 * @returns {Promise<boolean>}
 */
const confirmDialog = async (title, text, icon = 'question') => {
  if (typeof Swal === 'undefined') return window.confirm(`${title}\n${text}`);
  const result = await Swal.fire({
    title,
    text,
    icon,
    showCancelButton: true,
    confirmButtonColor: '#29b44a',
    cancelButtonColor: '#6c757d',
    confirmButtonText: 'Sí, continuar',
    cancelButtonText: 'Cancelar',
    customClass: { popup: 'rounded-4 shadow-lg border-0' },
  });
  return result.isConfirmed;
};

/**
 * Alerta informativa con SweetAlert2.
 * @param {string} title
 * @param {string} text
 * @param {'success'|'error'|'warning'|'info'} icon
 */
const alertDialog = (title, text, icon = 'success') => {
  if (typeof Swal === 'undefined') { alert(`${title}: ${text}`); return; }
  Swal.fire({ title, text, icon, confirmButtonColor: '#29b44a', customClass: { popup: 'rounded-4 shadow-lg border-0' } });
};

/**
 * Reemplaza el contenido de un elemento con un skeleton loader.
 * @param {HTMLElement} el
 * @param {number} rows
 */
const showSkeleton = (el, rows = 3) => {
  el.innerHTML = Array.from({ length: rows }, () =>
    '<div class="skeleton mb-2" style="height:18px;width:' + (Math.random() * 30 + 60).toFixed(0) + '%"></div>'
  ).join('');
};

/**
 * Spinner Bootstrap pequeño para botones.
 * @param {HTMLElement} btn
 * @param {string} loadingText
 * @returns {Function} Restore function
 */
const btnLoading = (btn, loadingText = 'Procesando...') => {
  const orig = btn.innerHTML;
  const origDisabled = btn.disabled;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${loadingText}`;
  return () => { btn.innerHTML = orig; btn.disabled = origDisabled; };
};

window.DzUI = { showToast, confirmDialog, alertDialog, showSkeleton, btnLoading };
