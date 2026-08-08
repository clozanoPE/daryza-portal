/**
 * Daryza VBS — main.js
 *
 * Bootstrap del portal: inicializa sidebar toggle, tooltips, loader
 * y comportamientos globales de navegación.
 * Sin lógica de negocio — solo UI shell.
 */
document.addEventListener('DOMContentLoaded', () => {
  // ── Loader de página ────────────────────────────────────────────────────
  const loader = document.getElementById('global-loader');
  if (loader) {
    window.addEventListener('load', () => loader.classList.add('loaded'));
    // Fallback: ocultar después de 2s aunque no dispare 'load'
    setTimeout(() => loader && loader.classList.add('loaded'), 2000);
  }

  // ── Sidebar Toggle ──────────────────────────────────────────────────────
  const menuToggle = document.getElementById('menu-toggle');
  const wrapper    = document.getElementById('wrapper');
  const backdrop   = document.getElementById('sidebar-backdrop');

  // En móvil, "toggled" significa sidebar VISIBLE (lógica invertida respecto
  // a desktop, ver daryza_style.css). El backdrop solo debe verse cuando
  // ambas condiciones se cumplen a la vez — por eso se resuelve en JS y no
  // solo con una regla CSS de media query.
  function actualizarBackdrop() {
    if (!backdrop || !wrapper) return;
    const abierto = wrapper.classList.contains('toggled') && window.innerWidth < 992;
    backdrop.classList.toggle('show', abierto);
  }

  if (menuToggle && wrapper) {
    menuToggle.addEventListener('click', (e) => {
      e.preventDefault();
      wrapper.classList.toggle('toggled');
      actualizarBackdrop();
      window.dispatchEvent(new Event('resize'));
    });
    // En móvil, el sidebar empieza oculto (toggled se maneja via CSS)
    if (window.innerWidth < 992) wrapper.classList.remove('toggled');
  }

  if (backdrop && wrapper) {
    backdrop.addEventListener('click', () => {
      wrapper.classList.remove('toggled');
      actualizarBackdrop();
    });
  }

  window.addEventListener('resize', actualizarBackdrop);
  actualizarBackdrop();

  // ── Bootstrap Tooltips ──────────────────────────────────────────────────
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    new bootstrap.Tooltip(el, { trigger: 'hover' });
  });

  // ── Marcar enlace activo en sidebar ─────────────────────────────────────
  const currentPath = window.location.pathname;
  document.querySelectorAll('.sidebar-nav__link[href]').forEach(link => {
    if (link.getAttribute('href') === currentPath) {
      link.classList.add('active');
    }
  });

  // ── Compatibilidad: función legacy toastMsg ──────────────────────────────
  window.toastMsg = (title, text, icon = 'success') => {
    if (typeof DzUI !== 'undefined') {
      DzUI.alertDialog(title, text, icon);
    } else if (typeof Swal !== 'undefined') {
      Swal.fire({ title, text, icon, confirmButtonColor: '#29b44a' });
    }
  };
});
