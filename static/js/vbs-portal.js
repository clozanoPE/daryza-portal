/**
 * VBS PORTAL — JavaScript
 * Daryza · Vendor Booking System
 *
 * Referencias a modelos REALES:
 *  Appointment.status: SOLICITADO | CONFIRMADA | RECHAZADO | CANCELADA | FINALIZADA
 *  Appointment.coa_pdf: FileField (un solo COA por cita)
 *  Ticket.estado: PROGRAMADO | EN_PLANTA | FINALIZADO | CANCELADO
 *  Ticket.requiere_coa: BooleanField
 *  TicketLineInspection.estado: PENDIENTE | EN_PROCESO | CONFORME | RECHAZADO
 *  TicketLineInspection.cantidad_modificada (campo real, no cantidad_real)
 *  TicketStage.etapa: VIGILANCIA_ENTRADA | ALMACEN_RECEPCION | CALIDAD_INSPECCION | VIGILANCIA_SALIDA
 *
 * URLs reales del proyecto:
 *  appointments: portal_proveedor, historial_entregas, solicitar_cita, subir_coa
 *  operations: panel_almacen, panel_calidad, panel_vigilancia (definidas en urls de operations)
 */

'use strict';

const VBS = (function () {

  /* ── CSRF ─────────────────────────────────────────── */
  function getCsrfToken() {
    const cookie = document.cookie.split(';').find(c => c.trim().startsWith('csrftoken='));
    if (cookie) return cookie.split('=')[1].trim();
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    const inp = document.querySelector('[name=csrfmiddlewaretoken]');
    return inp ? inp.value : '';
  }

  /* ── CLOCK ────────────────────────────────────────── */
  const Clock = {
    init() {
      const el = document.getElementById('vbs-clock');
      if (!el) return;
      const tick = () => {
        const n = new Date();
        el.textContent =
          n.toLocaleDateString('es-PE', { weekday:'short', day:'2-digit', month:'short' })
          + '  '
          + n.toLocaleTimeString('es-PE', { hour:'2-digit', minute:'2-digit', second:'2-digit' });
      };
      tick();
      setInterval(tick, 1000);
    }
  };

  /* ── MESSAGES ─────────────────────────────────────── */
  const Messages = {
    _container: null,

    init() {
      this._container = document.getElementById('vbs-messages');
      if (!this._container) {
        this._container = document.createElement('div');
        this._container.id = 'vbs-messages';
        this._container.className = 'messages-container';
        document.body.appendChild(this._container);
      }
      // Auto-dismiss existing Django messages rendered server-side
      document.querySelectorAll('.message-toast').forEach(t => this._autoDismiss(t));
    },

    show(text, type = 'success') {
      const icons = {
        success: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`,
        error:   `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
        warning: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>`,
        info:    `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/></svg>`,
      };
      const toast = document.createElement('div');
      toast.className = `message-toast ${type}`;
      toast.innerHTML = `${icons[type] || ''}<span class="msg-text">${text}</span>`;
      this._container.appendChild(toast);
      this._autoDismiss(toast);
      return toast;
    },

    _autoDismiss(el, ms = 4200) {
      el.addEventListener('click', () => this._remove(el));
      setTimeout(() => this._remove(el), ms);
    },

    _remove(el) {
      if (!el || !el.parentNode) return;
      el.style.transition = 'opacity .3s, transform .3s';
      el.style.opacity = '0';
      el.style.transform = 'translateX(20px)';
      setTimeout(() => el.remove(), 300);
    }
  };

  /* ── MODAL ────────────────────────────────────────── */
  const Modal = {
    _active: null,

    init() {
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && this._active) this.close(this._active);
      });
      document.addEventListener('click', e => {
        if (e.target.classList.contains('modal-overlay') && this._active) this.close(this._active);
      });
    },

    open(id) {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.add('show');
      this._active = id;
      document.body.style.overflow = 'hidden';
    },

    close(id) {
      const el = document.getElementById(id || this._active);
      if (!el) return;
      el.classList.remove('show');
      if (this._active === id) { this._active = null; document.body.style.overflow = ''; }
    },

    closeAll() {
      document.querySelectorAll('.modal-overlay.show').forEach(el => el.classList.remove('show'));
      this._active = null;
      document.body.style.overflow = '';
    }
  };

  /* ── ACCORDION ────────────────────────────────────── */
  const Accordion = {
    init() {
      document.querySelectorAll('.accordion-header').forEach(btn => {
        btn.addEventListener('click', () => {
          const expanded = btn.getAttribute('aria-expanded') === 'true';
          const body = btn.nextElementSibling;
          if (!body) return;
          btn.setAttribute('aria-expanded', String(!expanded));
          body.classList.toggle('open', !expanded);
        });
      });
    }
  };

  /* ── AJAX ─────────────────────────────────────────── */
  const Ajax = {
    async post(url, data) {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
        body: JSON.stringify(data),
      });
      return resp.json();
    },

    async postForm(url, formData) {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCsrfToken() },
        body: formData,
      });
      return resp.json();
    }
  };

  /* ── AJAX FORMS ───────────────────────────────────── */
  const Forms = {
    init() {
      document.querySelectorAll('form[data-ajax="true"]').forEach(form => {
        form.addEventListener('submit', e => this._handle(e, form));
      });
    },

    async _handle(e, form) {
      e.preventDefault();
      const btn = form.querySelector('[type="submit"]');
      const orig = btn ? btn.innerHTML : '';
      if (btn) { btn.disabled = true; btn.innerHTML = `<span class="vbs-spinner"></span>`; }

      try {
        const result = await Ajax.postForm(form.action, new FormData(form));
        if (result.status === 'success') {
          Messages.show(result.msg, 'success');
          Modal.closeAll();
          const redir = form.dataset.redirect || result.redirect;
          if (redir) { setTimeout(() => { window.location.href = redir; }, 700); }
          else if (form.dataset.reload !== 'false') { setTimeout(() => window.location.reload(), 700); }
        } else {
          Messages.show(result.msg || 'Error al procesar.', 'error');
        }
      } catch (_) {
        Messages.show('Error de conexión.', 'error');
      } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = orig; }
      }
    }
  };

  /* ── CONFIRM ──────────────────────────────────────── */
  const Confirm = {
    init() {
      document.querySelectorAll('[data-confirm-url]').forEach(btn => {
        btn.addEventListener('click', () => this._show(btn));
      });
    },

    _show(btn) {
      const modal  = document.getElementById('vbs-confirm-modal');
      const url    = btn.dataset.confirmUrl;
      const title  = btn.dataset.confirmTitle   || '¿Confirmar?';
      const msg    = btn.dataset.confirmMessage || 'Esta acción no se puede deshacer.';
      const type   = btn.dataset.confirmType    || 'primary';
      const label  = btn.dataset.confirmLabel   || 'Confirmar';
      const payload= btn.dataset.confirmPayload ? JSON.parse(btn.dataset.confirmPayload) : {};

      if (!modal) { this._exec(url, payload); return; }

      modal.querySelector('#confirm-title').textContent   = title;
      modal.querySelector('#confirm-message').textContent = msg;
      const actionBtn = modal.querySelector('#confirm-action-btn');
      const newBtn = actionBtn.cloneNode(true);
      actionBtn.parentNode.replaceChild(newBtn, actionBtn);
      newBtn.textContent = label;
      newBtn.className   = `btn btn-${type}`;
      newBtn.addEventListener('click', () => this._exec(url, payload));
      Modal.open('vbs-confirm-modal');
    },

    async _exec(url, payload) {
      Modal.closeAll();
      try {
        const result = await Ajax.post(url, payload);
        Messages.show(result.msg || 'Acción ejecutada.', result.status === 'success' ? 'success' : 'error');
        if (result.status === 'success') setTimeout(() => window.location.reload(), 800);
      } catch (_) {
        Messages.show('Error de conexión.', 'error');
      }
    }
  };

  /* ── SLOT MATRIX ──────────────────────────────────── */
  const Slots = {
    init() {
      document.querySelectorAll('.slot-cell[data-slot-id]').forEach(cell => {
        if (!cell.classList.contains('slot-full') && !cell.classList.contains('slot-blocked')) {
          cell.addEventListener('click', () => this._select(cell));
        }
      });
    },

    _select(cell) {
      document.querySelectorAll('.slot-cell.selected').forEach(c => c.classList.remove('selected'));
      cell.classList.add('selected');

      const hidden = document.getElementById('id_slot_id');
      if (hidden) hidden.value = cell.dataset.slotId;

      const display = document.getElementById('slot-selected-display');
      if (display) {
        display.textContent = `${cell.dataset.fecha} · ${cell.dataset.hora} — Muelle ${cell.dataset.dock} — ${cell.dataset.disponibles} disponible(s)`;
        display.style.color = 'var(--vbs-cyan)';
      }

      // Habilitar botón de submit si hay OC seleccionada
      _checkSolicitudReady();
    }
  };

  /* ── COA UPLOAD (Appointment.coa_pdf — campo real) ── */
  const CoaUpload = {
    init() {
      // Zona de carga con drag & drop
      document.querySelectorAll('.coa-upload-zone').forEach(zone => {
        const input = zone.querySelector('input[type="file"]');
        if (!input) return;

        zone.addEventListener('click', () => input.click());
        zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
        zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
        zone.addEventListener('drop', e => {
          e.preventDefault(); zone.classList.remove('drag-over');
          const file = e.dataTransfer.files[0];
          if (file) this._handle(input, file, zone);
        });
        input.addEventListener('change', () => { if (input.files[0]) this._handle(input, input.files[0], zone); });
      });

      // Inputs de archivo sueltos con data-upload-url
      document.querySelectorAll('.coa-file-input').forEach(input => {
        input.addEventListener('change', () => {
          if (input.files[0]) this._autoUpload(input);
        });
      });
    },

    _handle(input, file, zone) {
      if (!file.name.toLowerCase().endsWith('.pdf')) { Messages.show('Solo se aceptan archivos PDF.', 'error'); return; }
      if (file.size > 10 * 1024 * 1024) { Messages.show('El archivo supera 10 MB.', 'error'); return; }
      const label = zone.querySelector('.coa-filename');
      if (label) label.textContent = file.name;
      const url = zone.dataset.uploadUrl;
      if (url) this._upload(url, input, zone);
    },

    async _autoUpload(input) {
      const url = input.dataset.uploadUrl;
      if (!url) return;
      await this._upload(url, input, null);
    },

    async _upload(url, input, zone) {
      if (zone) zone.classList.add('uploading');
      const fd = new FormData();
      fd.append('coa_pdf', input.files[0]);
      // po_line_id si existe (para futuras extensiones)
      if (input.dataset.poLineId) fd.append('po_line_id', input.dataset.poLineId);
      try {
        const result = await Ajax.postForm(url, fd);
        if (result.status === 'success') {
          Messages.show('COA cargado correctamente.', 'success');
          setTimeout(() => window.location.reload(), 800);
        } else {
          Messages.show(result.msg || 'Error al cargar COA.', 'error');
        }
      } catch (_) {
        Messages.show('Error de conexión.', 'error');
      } finally {
        if (zone) zone.classList.remove('uploading');
      }
    }
  };

  /* ── QR SCANNER ───────────────────────────────────── */
  const QRScanner = {
    init() {
      const form = document.getElementById('qr-scan-form');
      if (!form) return;
      const input = document.getElementById('qr-input');
      const btn   = document.getElementById('qr-submit-btn');

      // Auto mayúsculas
      if (input) {
        input.addEventListener('input', () => {
          const pos = input.selectionStart;
          input.value = input.value.toUpperCase();
          input.setSelectionRange(pos, pos);
        });
        // Focus automático
        setTimeout(() => input.focus(), 200);
      }

      form.addEventListener('submit', async e => {
        e.preventDefault();
        const qr = input ? input.value.trim() : '';
        if (!qr) { Messages.show('Ingrese el código QR.', 'warning'); return; }

        if (btn) { btn.disabled = true; btn.innerHTML = `<span class="vbs-spinner"></span> Procesando...`; }

        try {
          const url = form.dataset.url;
          const result = await Ajax.post(url, { qr_code: qr });
          if (result.status === 'success') {
            Messages.show(result.msg, 'success');
            if (input) input.value = '';
            setTimeout(() => window.location.reload(), 1000);
          } else {
            Messages.show(result.msg || 'QR no válido.', 'error');
          }
        } catch (_) {
          Messages.show('Error de conexión.', 'error');
        } finally {
          if (btn) { btn.disabled = false; btn.innerHTML = `Registrar Ingreso`; }
        }
      });
    }
  };

  /* ── INSPECCION — diff en tiempo real ────────────── */
  /* Campo real del modelo: cantidad_modificada (no cantidad_real) */
  const Inspeccion = {
    init() {
      document.querySelectorAll('.insp-qty-input').forEach(input => {
        input.addEventListener('input', () => {
          const row = input.closest('[data-sap-qty]');
          if (!row) return;
          const sap  = parseFloat(row.dataset.sapQty) || 0;
          const real = parseFloat(input.value) || 0;
          const diff = real - sap;
          const el   = row.querySelector('.insp-diff');
          if (!el) return;
          el.textContent = diff !== 0 ? (diff > 0 ? `+${diff.toFixed(3)}` : diff.toFixed(3)) : '—';
          el.style.color  = diff < 0 ? 'var(--vbs-red)' : diff > 0 ? 'var(--vbs-amber)' : 'var(--vbs-green)';
        });
      });
    }
  };

  /* ── SOLICITUD LISTA (habilitar botón submit) ─────── */
  function _checkSolicitudReady() {
    const btn     = document.getElementById('btn-solicitar');
    const slotVal = document.getElementById('id_slot_id');
    const hasOc   = document.querySelector('input[name="oc_ids"]:checked');
    if (btn && slotVal) btn.disabled = !(slotVal.value && hasOc);
  }

  /* ── INIT ─────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', () => {
    Clock.init();
    Messages.init();
    Modal.init();
    Accordion.init();
    Forms.init();
    Confirm.init();
    Slots.init();
    CoaUpload.init();
    QRScanner.init();
    Inspeccion.init();

    // OC checkboxes → verificar botón solicitud
    document.querySelectorAll('input[name="oc_ids"]').forEach(cb => {
      cb.addEventListener('change', _checkSolicitudReady);
    });
  });

  /* API pública */
  return { Modal, Messages, Ajax, getCsrfToken };

})();
