/* static/js/panel_horarios.js
 *
 * NUEVO ARCHIVO — Fase 3: Panel de Administración de Horarios
 *
 * CAPAS AFECTADAS:
 *   - templates/scheduling/panel_horarios.html → lo carga en {% block extra_js %}
 *   - apps/scheduling/views.py (ajax_*) → todos los endpoints que consume este script
 *   - base.html → ya incluye Bootstrap 5, SweetAlert2 y Bootstrap Icons (sin cambios)
 *
 * VARIABLES GLOBALES ESPERADAS (inyectadas por el template):
 *   SLOTS_DATA    : Array de objetos slot {id, fecha, dia_nombre, hora, max_capacity,
 *                   citas_count, is_full_override, estado, puede_eliminarse}
 *   DIAS_SEMANA   : Array [{fecha, nombre, fecha_display}] — 6 elementos (L-S)
 *   SEMANA_ACTUAL : String 'YYYY-MM-DD' del lunes de la semana visible
 *   CSRF_TOKEN    : Token CSRF para peticiones POST
 */

'use strict';

// ── Renderizado de la grilla ───────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {
    renderizarGrilla(SLOTS_DATA, DIAS_SEMANA);
});

/**
 * Construye la tabla HTML de la grilla semanal.
 *
 * Agrupa los slots por hora, luego por fecha (columna del día).
 * Las celdas vacías (sin slot para ese día/hora) muestran un placeholder.
 *
 * @param {Array} slots   - Datos de SLOTS_DATA
 * @param {Array} dias    - Datos de DIAS_SEMANA
 */
function renderizarGrilla(slots, dias) {
    const tbody = document.getElementById('grilla-body');

    if (slots.length === 0) {
        tbody.innerHTML = `
            <tr>
              <td colspan="7" class="text-center py-5 text-muted">
                <i class="bi bi-calendar-x fs-2 d-block mb-2 opacity-25"></i>
                No hay slots programados para esta semana.
                <br><small>Usa "Generar desde plantilla" o "Añadir slot" para comenzar.</small>
              </td>
            </tr>`;
        return;
    }

    // Agrupar slots por hora → por fecha
    const porHora = {};
    slots.forEach(slot => {
        if (!porHora[slot.hora]) porHora[slot.hora] = {};
        porHora[slot.hora][slot.fecha] = slot;
    });

    const horas = Object.keys(porHora).sort();
    let html = '';

    horas.forEach(hora => {
        html += `<tr><td class="hora-label ps-2">${hora}</td>`;

        dias.forEach(dia => {
            const slot = porHora[hora][dia.fecha];
            if (slot) {
                html += `<td>${construirCeldaSlot(slot)}</td>`;
            } else {
                // Celda vacía: permite añadir slot desde aquí
                html += `<td>
                    <div class="slot-vacio d-flex justify-content-center align-items-center"
                         title="Sin slot programado"
                         onclick="abrirModalAgregarSlotConFecha('${dia.fecha}', '${hora}')">
                      <i class="bi bi-plus text-muted opacity-50"></i>
                    </div>
                  </td>`;
            }
        });

        html += `</tr>`;
    });

    tbody.innerHTML = html;
}

/**
 * Construye el HTML de una celda de slot con su estado, capacidad y acciones.
 *
 * @param {Object} slot - Objeto slot de SLOTS_DATA
 * @returns {string} HTML de la celda
 */
function construirCeldaSlot(slot) {
    // Clase CSS según estado del slot
    const claseEstado = {
        'disponible': 'slot-disponible',
        'parcial':    'slot-parcial',
        'lleno':      'slot-lleno',
        'bloqueado':  'slot-bloqueado',
    }[slot.estado] || 'slot-disponible';

    // Icono de estado
    const iconoEstado = {
        'disponible': 'bi-check-circle',
        'parcial':    'bi-circle-half',
        'lleno':      'bi-x-circle',
        'bloqueado':  'bi-lock-fill',
    }[slot.estado] || 'bi-circle';

    // Botones de acción (aparecen en hover vía CSS)
    const btnBloqueo = slot.is_full_override
        ? `<button class="btn btn-sm btn-outline-success" onclick="toggleBloqueo(${slot.id}, 'desbloquear', event)"
             title="Desbloquear"><i class="bi bi-unlock"></i></button>`
        : `<button class="btn btn-sm btn-outline-warning" onclick="toggleBloqueo(${slot.id}, 'bloquear', event)"
             title="Bloquear"><i class="bi bi-lock"></i></button>`;

    const btnEliminar = slot.puede_eliminarse
        ? `<button class="btn btn-sm btn-outline-danger" onclick="eliminarSlot(${slot.id}, event)"
             title="Eliminar slot"><i class="bi bi-trash3"></i></button>`
        : '';

    const disponibles = slot.max_capacity - slot.citas_count;

    return `
        <div class="slot-cell ${claseEstado}" onclick="verDetalleSlot(${slot.id}, '${slot.fecha}', '${slot.hora}', ${slot.citas_count})">
          <div class="slot-actions">
            ${btnBloqueo}
            ${btnEliminar}
          </div>
          <i class="bi ${iconoEstado} mb-1" style="font-size:1rem"></i>
          <div style="font-size:0.7rem;font-weight:700">${slot.citas_count}/${slot.max_capacity}</div>
          <div class="cap-badge">${disponibles > 0 ? disponibles + ' libre(s)' : 'Sin cupo'}</div>
        </div>`;
}


// ── Acciones AJAX ─────────────────────────────────────────────────────────────

/**
 * Genera los slots de la semana actual desde la plantilla seleccionada.
 * Endpoint: POST /scheduling/api/generar-semana/
 */
function generarSemana() {
    const templateId = document.getElementById('select-template').value;
    if (!templateId) {
        toastMsg('Selección requerida', 'Debes elegir una plantilla de horario.', 'warning');
        return;
    }

    const btn = document.getElementById('btn-generar');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Generando...';

    ajaxPost('/scheduling/api/generar-semana/', {
        template_id: parseInt(templateId),
        lunes: SEMANA_ACTUAL,
    })
    .then(data => {
        bootstrap.Modal.getInstance(document.getElementById('modalGenerarSemana'))?.hide();
        toastMsg('¡Semana generada!', data.msg, 'success');
        setTimeout(() => location.reload(), 1600);
    })
    .catch(msg => {
        toastMsg('Error al generar', msg, 'error');
    })
    .finally(() => {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check2-circle me-1"></i> Generar';
    });
}

/**
 * Solicita confirmación y duplica la semana anterior hacia la semana actual.
 * Endpoint: POST /scheduling/api/duplicar-semana/
 *
 * @param {string} lunesOrigen   - Fecha YYYY-MM-DD de la semana a copiar
 * @param {string} semanaDestino - Label legible de la semana destino (para el mensaje)
 */
function confirmarDuplicar(lunesOrigen, semanaDestino) {
    Swal.fire({
        icon: 'question',
        title: 'Duplicar semana anterior',
        html: `Se copiarán todos los slots de la semana anterior a la semana del <strong>${semanaDestino}</strong>.<br>
               <small class="text-muted">Los bloqueos manuales <u>no</u> se copian. Los slots ya existentes no se sobreescriben.</small>`,
        showCancelButton: true,
        confirmButtonColor: '#198754',
        cancelButtonText: 'Cancelar',
        confirmButtonText: '<i class="bi bi-copy me-1"></i> Sí, duplicar',
        customClass: { popup: 'rounded-4 shadow-lg border-0' }
    }).then(result => {
        if (!result.isConfirmed) return;

        ajaxPost('/scheduling/api/duplicar-semana/', { lunes_origen: lunesOrigen })
        .then(data => {
            toastMsg('¡Semana duplicada!', data.msg, 'success');
            setTimeout(() => location.reload(), 1800);
        })
        .catch(msg => toastMsg('Error al duplicar', msg, 'error'));
    });
}

/**
 * Bloquea o desbloquea un slot.
 * Endpoint: POST /scheduling/api/toggle-bloqueo/
 *
 * @param {number} slotId  - ID del AppointmentSlot
 * @param {string} accion  - 'bloquear' | 'desbloquear'
 * @param {Event}  e       - Evento del click (para evitar propagación al padre)
 */
function toggleBloqueo(slotId, accion, e) {
    e.stopPropagation();  // Evita que se abra el modal de detalle

    ajaxPost('/scheduling/api/toggle-bloqueo/', { slot_id: slotId, accion: accion })
    .then(data => {
        toastMsg(
            accion === 'bloquear' ? 'Slot bloqueado' : 'Slot desbloqueado',
            data.msg,
            'success'
        );
        setTimeout(() => location.reload(), 1200);
    })
    .catch(msg => toastMsg('Error', msg, 'error'));
}

/**
 * Elimina un slot vacío tras confirmación.
 * Endpoint: POST /scheduling/api/eliminar-slot/
 *
 * @param {number} slotId - ID del AppointmentSlot
 * @param {Event}  e      - Evento del click
 */
function eliminarSlot(slotId, e) {
    e.stopPropagation();

    Swal.fire({
        icon: 'warning',
        title: '¿Eliminar slot?',
        text: 'Esta acción es permanente. Solo es posible si el slot no tiene citas activas.',
        showCancelButton: true,
        confirmButtonColor: '#dc3545',
        cancelButtonText: 'Cancelar',
        confirmButtonText: 'Eliminar',
        customClass: { popup: 'rounded-4 shadow-lg border-0' }
    }).then(result => {
        if (!result.isConfirmed) return;

        ajaxPost('/scheduling/api/eliminar-slot/', { slot_id: slotId })
        .then(data => {
            toastMsg('Slot eliminado', data.msg, 'success');
            setTimeout(() => location.reload(), 1200);
        })
        .catch(msg => toastMsg('Error al eliminar', msg, 'error'));
    });
}

/**
 * Añade un slot individual.
 * Endpoint: POST /scheduling/api/agregar-slot/
 */
function agregarSlot() {
    const fecha     = document.getElementById('nuevo-slot-fecha').value;
    const hora      = document.getElementById('nuevo-slot-hora').value;
    const capacidad = document.getElementById('nuevo-slot-capacidad').value;

    if (!fecha || !hora) {
        toastMsg('Datos incompletos', 'Debes ingresar fecha y hora.', 'warning');
        return;
    }

    ajaxPost('/scheduling/api/agregar-slot/', { fecha, hora, capacidad: parseInt(capacidad) })
    .then(data => {
        bootstrap.Modal.getInstance(document.getElementById('modalAgregarSlot'))?.hide();
        toastMsg('Slot agregado', data.msg, 'success');
        setTimeout(() => location.reload(), 1400);
    })
    .catch(msg => toastMsg('Error al agregar', msg, 'error'));
}

/**
 * Pre-llena el modal de agregar slot con fecha y hora al hacer click
 * en una celda vacía de la grilla.
 */
function abrirModalAgregarSlotConFecha(fecha, hora) {
    document.getElementById('nuevo-slot-fecha').value = fecha;
    document.getElementById('nuevo-slot-hora').value  = hora;
    new bootstrap.Modal(document.getElementById('modalAgregarSlot')).show();
}

/**
 * Abre el panel lateral (offcanvas) con el detalle de las citas de un slot.
 * Si el slot no tiene citas, no abre nada: solo avisa con un toast.
 * Endpoint: GET /scheduling/api/citas-slot/<slotId>/
 *
 * @param {number} slotId
 * @param {string} fecha      - 'YYYY-MM-DD', solo para el título del panel
 * @param {string} hora       - 'HH:MM', solo para el título del panel
 * @param {number} citasCount - slot.citas_count, ya calculado por el backend
 */
function verDetalleSlot(slotId, fecha, hora, citasCount) {
    if (!citasCount) {
        toastMsg('Sin citas', 'Este horario todavía no tiene citas agendadas.', 'info');
        return;
    }

    const label = document.getElementById('offcanvasSlotDetalleLabel');
    const body  = document.getElementById('offcanvas-slot-body');
    label.innerHTML = `<i class="bi bi-people text-success me-2"></i>Citas — ${fecha} ${hora}`;
    body.innerHTML = `
        <div class="text-center py-4 text-muted">
          <div class="spinner-border spinner-border-sm text-success"></div>
        </div>`;

    bootstrap.Offcanvas.getOrCreateInstance(document.getElementById('offcanvasSlotDetalle')).show();

    fetch(`/scheduling/api/citas-slot/${slotId}/`)
        .then(async response => {
            const data = await response.json();
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.msg || 'No se pudo cargar el detalle del slot.');
            }
            return data;
        })
        .then(data => {
            body.innerHTML = construirListaCitas(data.citas);
        })
        .catch(err => {
            body.innerHTML = `<div class="alert alert-danger small mb-0">${err.message}</div>`;
        });
}

/**
 * Construye el HTML de la lista de citas del panel lateral.
 * Cada ítem enlaza a operations:trazabilidad_ticket (Fase 5) cuando la cita
 * ya tiene Ticket generado; si aún está SOLICITADO (sin confirmar por
 * Compras), no hay ticket al que enlazar y se muestra solo el estado.
 *
 * @param {Array} citas - [{appointment_id, proveedor_nombre, proveedor_codigo,
 *                          ticket_id, estado_display}]
 * @returns {string} HTML
 */
function construirListaCitas(citas) {
    if (!citas.length) {
        return '<p class="text-muted text-center small py-3 mb-0">Sin citas en este horario.</p>';
    }

    return '<div class="list-group list-group-flush">' + citas.map(c => `
        <div class="list-group-item px-0 py-3">
          <div class="d-flex justify-content-between align-items-start gap-2">
            <div>
              <span class="fw-bold d-block">${c.proveedor_nombre}</span>
              <small class="text-muted">${c.proveedor_codigo}</small>
            </div>
            <span class="badge bg-light text-dark border text-xxs">${c.estado_display}</span>
          </div>
          <div class="d-flex justify-content-between align-items-center mt-2">
            <small class="text-muted">
              ${c.ticket_id ? 'Ticket #' + c.ticket_id : 'Cita #' + c.appointment_id + ' (sin confirmar)'}
            </small>
            ${c.ticket_id
                ? `<a href="/operations/ticket/${c.ticket_id}/trazabilidad/" class="btn btn-sm btn-outline-success rounded-3" target="_blank">
                     <i class="bi bi-clock-history me-1"></i>Ver
                   </a>`
                : ''}
          </div>
        </div>`).join('') + '</div>';
}


// ── Helper AJAX ───────────────────────────────────────────────────────────────

/**
 * Wrapper de fetch para peticiones POST JSON con CSRF y manejo de errores estándar.
 * Retorna una Promise con el campo 'msg' en caso de error.
 *
 * @param {string} url   - URL del endpoint
 * @param {Object} body  - Payload a serializar como JSON
 * @returns {Promise}
 */
function ajaxPost(url, body) {
    return fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': CSRF_TOKEN,
        },
        body: JSON.stringify(body),
    })
    .then(async response => {
        const data = await response.json();
        if (!response.ok) throw data.msg || 'Error desconocido del servidor.';
        return data;
    });
}
