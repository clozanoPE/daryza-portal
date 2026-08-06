/**
 * Persistencia de la pestaña activa (Bootstrap nav-tabs, patrón
 * Pendientes|Hoy / Historial) al enviar un formulario de la página.
 *
 * Sin esto, cualquier <form method="get"> de la página (p.ej. el filtro
 * de período dentro de "Historial") recarga la página y Bootstrap vuelve
 * a mostrar la pestaña marcada como "active" en el HTML (siempre la
 * primera), perdiendo la pestaña en la que el usuario estaba.
 *
 * Usado por panel_compras / panel_vigilancia / panel_calidad.
 */
(function () {
  function tabIdActivo() {
    var activo = document.querySelector('.nav-tabs .nav-link.active');
    if (!activo) return null;
    return activo.id.replace(/^tab-/, '').replace(/-btn$/, '');
  }

  document.addEventListener('DOMContentLoaded', function () {
    var params = new URLSearchParams(window.location.search);
    var tab = params.get('tab');
    if (tab) {
      var btn = document.getElementById('tab-' + tab + '-btn');
      if (btn && window.bootstrap) {
        new bootstrap.Tab(btn).show();
      }
    }

    document.querySelectorAll('form').forEach(function (form) {
      form.addEventListener('submit', function () {
        var input = form.querySelector('input[name="tab"]');
        if (!input) {
          input = document.createElement('input');
          input.type = 'hidden';
          input.name = 'tab';
          form.appendChild(input);
        }
        input.value = tabIdActivo() || '';
      });
    });
  });
})();
