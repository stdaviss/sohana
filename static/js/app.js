/* ── NUMBER FORMATTING ── */
/**
 * Format a numeric value as "x xxx xxx.xx" with space-separated thousands.
 * @param {number} value   — the number (already in display units, e.g. 1234.56)
 * @param {string} symbol  — currency symbol, e.g. "€" (default "€")
 * @returns {string}       — e.g. "€1 234 567.89"
 */
function fmtMoney(value, symbol = '€') {
  const num = parseFloat(value) || 0;
  const [int, dec] = num.toFixed(2).split('.');
  // Add space every 3 digits from the right
  const intFormatted = int.replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0');
  return `${symbol}${intFormatted}.${dec}`;
}

/**
 * Format cents to display string.
 * @param {number} cents
 * @param {string} symbol
 */
function fmtCents(cents, symbol = '€') {
  return fmtMoney(Math.abs(cents) / 100, symbol);
}


/* ── TOAST ── */
Global JS v3 */
const API = {
  async post(url, data) {
    const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) });
    return r.json();
  },
  async get(url) { return (await fetch(url)).json(); }
};

function toast(msg, type='info') {
  let el = document.getElementById('_toast');
  if (!el) { el = document.createElement('div'); el.id='_toast'; el.className='toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.className = 'toast show' + (type !== 'info' ? ' '+type : '');
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), 3400);
}

function openModal(id) { document.getElementById(id)?.classList.add('open'); }
function closeModal(id) { document.getElementById(id)?.classList.remove('open'); }
document.addEventListener('click', e => { if (e.target.classList.contains('modal-backdrop')) e.target.classList.remove('open'); });

function setLoading(btn, loading) {
  if (!btn) return;
  if (loading) { btn._txt = btn.innerHTML; btn.innerHTML = '<span class="spinner-sm"></span>'; btn.disabled = true; }
  else { btn.innerHTML = btn._txt || btn.innerHTML; btn.disabled = false; }
}

function sparkline(id, data, color) {
  const canvas = document.getElementById(id);
  if (!canvas || !data || data.length < 2) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width  = rect.width  * dpr;
  canvas.height = (parseInt(canvas.getAttribute('height')) || 120) * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = parseInt(canvas.getAttribute('height')) || 120;
  const scores = data.map(d => typeof d === 'object' ? (d.score || d.value || 0) : d);
  const minS = Math.max(0, Math.min(...scores)-30), maxS = Math.max(...scores)+30;
  const tx = i => 8 + (i/(scores.length-1))*(W-16);
  const ty = s => H-8 - ((s-minS)/(maxS-minS||1))*(H-16);
  const grad = ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0, color+'40'); grad.addColorStop(1, color+'05');
  ctx.beginPath(); ctx.moveTo(tx(0),H-8);
  scores.forEach((s,i) => ctx.lineTo(tx(i),ty(s)));
  ctx.lineTo(tx(scores.length-1),H-8); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  scores.forEach((s,i) => i===0 ? ctx.moveTo(tx(i),ty(s)) : ctx.lineTo(tx(i),ty(s)));
  ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.lineJoin='round'; ctx.lineCap='round'; ctx.stroke();
  const lx = tx(scores.length-1), ly = ty(scores[scores.length-1]);
  ctx.beginPath(); ctx.arc(lx,ly,4,0,Math.PI*2); ctx.fillStyle=color; ctx.fill();
}

async function doLogout() {
  await API.post('/api/auth/logout', {});
  window.location.href = '/';
}

/* Notification popup panel */
let notifPanelOpen = false;
async function toggleNotifPanel() {
  const panel = document.getElementById('notif-panel');
  if (!panel) return;
  notifPanelOpen = !notifPanelOpen;
  panel.style.display = notifPanelOpen ? 'block' : 'none';
  if (notifPanelOpen) {
    await loadNotifPanel();
    await API.post('/api/notifications/mark-read', {});
    const badge = document.getElementById('notif-badge');
    if (badge) badge.style.display = 'none';
  }
}
document.addEventListener('click', e => {
  const panel = document.getElementById('notif-panel');
  const btn   = document.getElementById('notif-btn');
  if (panel && notifPanelOpen && !panel.contains(e.target) && e.target !== btn && !btn?.contains(e.target)) {
    panel.style.display = 'none';
    notifPanelOpen = false;
  }
});
async function loadNotifPanel() {
  const list = document.getElementById('notif-list');
  if (!list) return;
  const d = await API.get('/api/notifications');
  if (!d.notifications || !d.notifications.length) {
    list.innerHTML = '<div style="padding:1.25rem;text-align:center;color:var(--text-muted);font-size:.83rem;">No notifications</div>';
    return;
  }
  list.innerHTML = d.notifications.slice(0,8).map(n => `
    <div onclick="${n.link ? "location.href='"+n.link+"'" : ''}" style="display:flex;gap:.6rem;padding:.7rem 1rem;border-bottom:1px solid var(--border);cursor:${n.link?'pointer':'default'};transition:background .1s;${!n.is_read?'background:var(--blue-lt);':''}" onmouseover="this.style.background='var(--gray-50)'" onmouseout="this.style.background='${!n.is_read?'var(--blue-lt)':''}'" >
      <div style="width:30px;height:30px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.8rem;background:${n.notif_type==='success'?'var(--green-lt)':n.notif_type==='danger'?'var(--red-lt)':'var(--blue-lt)'};">
        ${n.notif_type==='success'?'✓':n.notif_type==='warning'?'⚠':'ℹ'}
      </div>
      <div style="flex:1;min-width:0;">
        <div style="font-size:.8rem;font-weight:600;color:var(--gray-800);">${n.title}</div>
        <div style="font-size:.75rem;color:var(--text-muted);line-height:1.4;margin-top:.1rem;">${n.body}</div>
        <div style="font-size:.68rem;color:var(--gray-400);margin-top:.2rem;">${n.created_at.slice(0,16).replace('T',' ')}</div>
      </div>
      ${!n.is_read?'<div style="width:7px;height:7px;border-radius:50%;background:var(--blue);flex-shrink:0;margin-top:3px;"></div>':''}
    </div>`).join('');
}
async function markAllRead() {
  await API.post('/api/notifications/mark-read', {});
  const badge = document.getElementById('notif-badge');
  if (badge) badge.style.display = 'none';
  await loadNotifPanel();
  toast('All marked as read');
}

/* Init */
document.addEventListener('DOMContentLoaded', async () => {
  document.querySelectorAll('[data-modal-open]').forEach(btn =>
    btn.addEventListener('click', () => openModal(btn.dataset.modalOpen)));
  /* Notification badge count */
  try {
    const d = await API.get('/api/notifications');
    const badge = document.getElementById('notif-badge');
    if (badge && d.unread > 0) {
      badge.textContent = d.unread > 9 ? '9+' : d.unread;
      badge.style.display = 'flex';
    }
  } catch(e) {}
});
