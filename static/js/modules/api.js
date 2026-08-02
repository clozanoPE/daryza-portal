/**
 * Daryza VBS — Módulo API
 * @module api
 *
 * Abstracción centralizada para todas las peticiones AJAX al backend.
 * Gestiona CSRF, errores de red y respuestas JSON de forma consistente.
 */

/** @type {string} Token CSRF leído del DOM al inicializar */
const getCsrfToken = () => {
  const el = document.querySelector('[name=csrfmiddlewaretoken]');
  if (el) return el.value;
  const cookie = document.cookie.split(';').find(c => c.trim().startsWith('csrftoken='));
  return cookie ? cookie.split('=')[1] : '';
};

/**
 * POST JSON genérico con CSRF.
 * @param {string} url
 * @param {Object} body
 * @returns {Promise<{status:string, msg?:string, [key:string]:any}>}
 */
const postJSON = async (url, body = {}) => {
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCsrfToken(),
    },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.msg || `HTTP ${resp.status}`);
  return data;
};

/**
 * GET JSON genérico.
 * @param {string} url
 * @returns {Promise<Object>}
 */
const getJSON = async (url) => {
  const resp = await fetch(url, {
    headers: { 'Accept': 'application/json' },
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.msg || `HTTP ${resp.status}`);
  return data;
};

/**
 * POST con FormData (archivos).
 * @param {string} url
 * @param {FormData} formData
 * @returns {Promise<Object>}
 */
const postFormData = async (url, formData) => {
  formData.append('csrfmiddlewaretoken', getCsrfToken());
  const resp = await fetch(url, { method: 'POST', body: formData });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.msg || `HTTP ${resp.status}`);
  return data;
};

// Exportamos para uso en módulos ES y acceso global
window.DzApi = { postJSON, getJSON, postFormData, getCsrfToken };
