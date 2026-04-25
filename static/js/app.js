/* SOHANA — Global JavaScript */

/* ── API ── */
const API = {
  async post(url, data) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
    return r.json();
  },
  async get(url) {
    return (await fetch(url)).json();
  }
};

/* ── NUMBER FORMATTING ── */
function fmtMoney(value, symbol) {
  symbol = symbol || '€';
  const num = parseFloat(value) || 0;
  const parts = num.toFixed(2).split('.');
  const intFormatted = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0');
  return symbol + intFormatted + '.' + parts[1];
}

function fmtCents(cents, symbol) {
  return fmtMoney(Math.abs(cents) / 100, symbol || '€');
}

/* ── TOAST ── */
function toast(msg, type) {
  type = type || 'info';
  let el = document.getElementById('_toast');
  if (!el) {
    el = document.createElement('div');
    el.id = '_toast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = 'toast show' + (type !== 'info' ? ' ' + type : '');
  clearTimeout(el._t);
  el._t = setTimeout(function() { el.classList.remove('show'); }, 3400);
}

/* ── MODAL ── */
function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
}

document.addEventListener('click', function(e) {
  if (e.target.classList.contains('modal-backdrop')) {
    e.target.classList.remove('open');
  }
});

/* ── LOADING BUTTON STATE ── */
function setLoading(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn._txt = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-sm"></span>';
    btn.disabled = true;
  } else {
    btn.innerHTML = btn._txt || btn.innerHTML;
    btn.disabled = false;
  }
}

/* ── SPARKLINE CHART ── */
function sparkline(canvasId, data, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !data || data.length < 2) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width  = rect.width * dpr;
  canvas.height = (parseInt(canvas.getAttribute('height')) || 120) * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width;
  const H = parseInt(canvas.getAttribute('height')) || 120;
  const scores = data.map(function(d) {
    return typeof d === 'object' ? (d.score || d.value || 0) : d;
  });
  const minS = Math.max(0, Math.min.apply(null, scores) - 30);
  const maxS = Math.max.apply(null, scores) + 30;
  const toX = function(i) { return 8 + (i / (scores.length - 1)) * (W - 16); };
  const toY = function(s) { return H - 8 - ((s - minS) / (maxS - minS)) * (H - 16); };
  ctx.beginPath();
  ctx.moveTo(toX(0), H - 8);
  scores.forEach(function(s, i) { ctx.lineTo(toX(i), toY(s)); });
  ctx.lineTo(toX(scores.length - 1), H - 8);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, color + '40');
  grad.addColorStop(1, color + '05');
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  scores.forEach(function(s, i) {
    i === 0 ? ctx.moveTo(toX(i), toY(s)) : ctx.lineTo(toX(i), toY(s));
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round';
  ctx.lineCap  = 'round';
  ctx.stroke();
  const lx = toX(scores.length - 1);
  const ly = toY(scores[scores.length - 1]);
  ctx.beginPath();
  ctx.arc(lx, ly, 4, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
}

/* ── NOTIFICATION PANEL ── */
let notifPanelOpen = false;

async function toggleNotifPanel() {
  const panel = document.getElementById('notif-panel');
  if (!panel) return;
  notifPanelOpen = !notifPanelOpen;
  panel.style.display = notifPanelOpen ? 'block' : 'none';
  if (notifPanelOpen) {
    try {
      const d = await API.get('/api/notifications');
      const list = document.getElementById('notif-list');
      if (!list) return;
      if (!d.notifications || !d.notifications.length) {
        list.innerHTML = '<div style="padding:1rem;text-align:center;color:#94a3b8;font-size:.85rem;">No notifications yet.</div>';
        return;
      }
      list.innerHTML = d.notifications.slice(0, 8).map(function(n) {
        const colors = { success: 'green', danger: 'red', warning: 'orange', info: 'blue' };
        const icon   = n.notif_type === 'success' ? '✓' : 'ℹ';
        const color  = colors[n.notif_type] || 'blue';
        const click  = n.link ? 'location.href=\'' + n.link + '\'' : '';
        const bg     = !n.is_read ? 'background:#eff6ff;' : '';
        return '<div class="notif-item" onclick="' + click + '" style="' + bg + 'cursor:' + (n.link ? 'pointer' : 'default') + ';">' +
          '<div class="notif-dot-icon ' + color + '">' + icon + '</div>' +
          '<div class="notif-body">' +
          '<div class="notif-text"><strong>' + n.title + '</strong><br>' + n.body + '</div>' +
          '<div class="notif-time">' + n.created_at.slice(0, 16).replace('T', ' ') + '</div>' +
          '</div><span class="notif-arrow">›</span></div>';
      }).join('');
      const badge = document.getElementById('notif-badge');
      if (badge) { badge.style.display = 'none'; }
      API.post('/api/notifications/mark-read', {});
    } catch (e) { /* silent fail */ }
  }
}

async function markAllRead() {
  await API.post('/api/notifications/mark-read', {});
  const badge = document.getElementById('notif-badge');
  if (badge) badge.style.display = 'none';
  toggleNotifPanel();
  toggleNotifPanel();
}

/* ── AUTH ── */
async function doLogout() {
  await API.post('/api/auth/logout', {});
  window.location.href = '/';
}

/* ── INIT ── */
document.addEventListener('DOMContentLoaded', function() {
  /* Load notification badge count */
  if (document.getElementById('notif-badge')) {
    API.get('/api/notifications').then(function(d) {
      const badge = document.getElementById('notif-badge');
      if (badge && d.unread > 0) {
        badge.textContent = d.unread;
        badge.style.display = 'flex';
      }
    }).catch(function() {});
  }
});
