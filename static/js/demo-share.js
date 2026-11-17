/*
 * SOHANA demo — share cards.
 * Visitors turn their balance, credit score or a receipt into a branded image
 * (1080×1350, Instagram/WhatsApp-status friendly) and share it with the phone's
 * share sheet, or via WhatsApp / X / LinkedIn / Facebook links, or save it.
 * Every card carries "Demo · simulated money" so nothing reads as a real balance.
 * Loaded only on the demo service (DEMO_MODE=1).
 */
(function () {
  if (window.dzShare) return;
  var C = { bg: '#0B0F0C', bg2: '#131915', bg3: '#1A221C', line: 'rgba(244,242,236,.12)', ink: '#F4F2EC', ink2: '#B9BDB6', ink3: '#868B84', mint: '#9EE493', amber: '#F4C06A' };
  var W = 1080, H = 1350, QR = null;
  fetch('/static/demo-qr.txt').then(function (r) { return r.ok ? r.text() : ''; }).then(function (t) { QR = (t || '').trim() || null; }).catch(function () {});

  /* ---------- styles ---------- */
  var css = document.createElement('style');
  css.textContent =
    '.dzs-fab{position:fixed;right:16px;bottom:calc(84px + env(safe-area-inset-bottom,0px));z-index:240;display:inline-flex;align-items:center;gap:8px;min-height:48px;padding:0 18px;border-radius:999px;border:0;background:#9EE493;color:#0B0F0C;font:600 15px Geist,system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.45);cursor:pointer}' +
    '@media(min-width:861px){.dzs-fab{bottom:24px;right:24px}}' +
    '.dzs-fab svg{width:18px;height:18px}' +
    '.dzs-bg{position:fixed;inset:0;z-index:2147483004;background:rgba(5,8,6,.78);backdrop-filter:blur(6px);display:flex;align-items:flex-end;justify-content:center;opacity:0;transition:opacity .2s}' +
    '.dzs-bg.on{opacity:1}' +
    '@media(min-width:700px){.dzs-bg{align-items:center}}' +
    '.dzs{width:min(520px,100%);max-height:94vh;overflow:auto;background:#131915;border:1px solid rgba(244,242,236,.14);border-radius:24px 24px 0 0;padding:18px 18px calc(18px + env(safe-area-inset-bottom,0px));color:#F4F2EC;font-family:Geist,system-ui,sans-serif;transform:translateY(30px);transition:transform .3s cubic-bezier(.2,.8,.2,1)}' +
    '.dzs-bg.on .dzs{transform:none}' +
    '@media(min-width:700px){.dzs{border-radius:24px}}' +
    '.dzs h3{font-size:18px;font-weight:600;letter-spacing:-.01em;margin:2px 0 4px}' +
    '.dzs p.sub{font-size:13.5px;color:#B9BDB6;margin:0 0 14px}' +
    '.dzs-pick{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}' +
    '.dzs-pick button{min-height:96px;border-radius:18px;border:1.5px solid rgba(244,242,236,.14);background:#1A221C;color:#F4F2EC;font:600 14px Geist,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;cursor:pointer}' +
    '.dzs-pick button svg{width:26px;height:26px;color:#9EE493}' +
    '.dzs-prev{width:100%;aspect-ratio:4/5;border-radius:18px;background:#0B0F0C;display:grid;place-items:center;overflow:hidden;border:1px solid rgba(244,242,236,.1)}' +
    '.dzs-prev img{width:100%;height:100%;object-fit:contain;display:block}' +
    '.dzs-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}' +
    '.dzs-row.four{grid-template-columns:repeat(4,1fr)}' +
    '.dzs-b{min-height:48px;border-radius:999px;border:1.5px solid rgba(244,242,236,.16);background:#1A221C;color:#F4F2EC;font:600 14px Geist,sans-serif;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;text-decoration:none}' +
    '.dzs-b.p{background:#9EE493;border-color:#9EE493;color:#0B0F0C}' +
    '.dzs-b.sm{min-height:42px;font-size:13px;padding:0 6px}' +
    '.dzs-x{float:right;width:36px;height:36px;border-radius:50%;border:0;background:#1A221C;color:#B9BDB6;font-size:18px;cursor:pointer}' +
    '.dzs-cap{margin-top:12px;font-size:13px;color:#B9BDB6;background:#0B0F0C;border:1px solid rgba(244,242,236,.1);border-radius:14px;padding:10px 12px;line-height:1.45}' +
    '.dzs-nudge{position:fixed;left:50%;bottom:calc(84px + env(safe-area-inset-bottom,0px));transform:translate(-50%,160%);z-index:2147483003;display:flex;align-items:center;gap:12px;background:rgba(26,34,28,.97);border:1px solid rgba(158,228,147,.4);border-radius:999px;padding:8px 8px 8px 18px;color:#F4F2EC;font:500 14px Geist,sans-serif;box-shadow:0 12px 30px rgba(0,0,0,.45);transition:transform .45s cubic-bezier(.2,.8,.2,1);white-space:nowrap}' +
    '.dzs-nudge.on{transform:translate(-50%,0)}' +
    '.dzs-nudge button{min-height:40px;padding:0 16px;border-radius:999px;border:0;background:#9EE493;color:#0B0F0C;font:600 14px Geist,sans-serif;cursor:pointer}';
  document.head.appendChild(css);

  var ICON = {
    share: '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/>',
    wallet: '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M16 13h2"/><path d="M5 6l10-3 2 3"/>',
    score: '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l4-5"/>',
    receipt: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>'
  };
  function svg(k) { return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' + ICON[k] + '</svg>'; }

  /* ---------- canvas helpers ---------- */
  function rr(x, c, y, w, h, r) { x.beginPath(); x.moveTo(c + r, y); x.arcTo(c + w, y, c + w, y + h, r); x.arcTo(c + w, y + h, c, y + h, r); x.arcTo(c, y + h, c, y, r); x.arcTo(c, y, c + w, y, r); x.closePath(); }
  function font(x, w, s, f) { x.font = w + ' ' + s + 'px ' + (f || 'Geist, "Helvetica Neue", Arial, sans-serif'); }
  function fit(x, text, max, w, s, f) { var n = s; font(x, w, n, f); while (x.measureText(text).width > max && n > 30) { n -= 4; font(x, w, n, f); } return n; }

  function frame(x, d) {
    x.fillStyle = C.bg; x.fillRect(0, 0, W, H);
    var g = x.createRadialGradient(W * 0.85, 0, 0, W * 0.85, 0, 900); g.addColorStop(0, 'rgba(158,228,147,.20)'); g.addColorStop(1, 'rgba(158,228,147,0)');
    x.fillStyle = g; x.fillRect(0, 0, W, H);
    // brand
    x.fillStyle = C.mint; rr(x, 72, 72, 72, 72, 18); x.fill();
    x.fillStyle = C.bg; font(x, '700', 40, '"Geist Mono", monospace'); x.textAlign = 'center'; x.textBaseline = 'middle'; x.fillText('S', 108, 110);
    x.textAlign = 'left'; x.fillStyle = C.ink; font(x, '700', 34); x.fillText('S O H A N A', 168, 110);
    x.textAlign = 'right'; x.fillStyle = C.mint; font(x, '600', 24, '"Geist Mono", monospace'); x.fillText('LIVE AT AFTS 2026 · KIGALI', W - 72, 110);
    x.textAlign = 'left'; x.textBaseline = 'alphabetic';
    // footer: QR + call to action
    x.strokeStyle = C.line; x.lineWidth = 2; x.beginPath(); x.moveTo(72, H - 270); x.lineTo(W - 72, H - 270); x.stroke();
    x.fillStyle = '#F4F2EC'; rr(x, 72, H - 230, 170, 170, 20); x.fill();
    if (QR && window.Path2D) {
      x.save(); x.translate(84, H - 218); x.scale(146 / 27, 146 / 27); x.translate(1, 1); x.fillStyle = C.bg; x.fill(new Path2D(QR)); x.restore();
    }
    x.fillStyle = C.ink; font(x, '600', 46); x.fillText('Try it in 30 seconds', 280, H - 175);
    x.fillStyle = C.mint; font(x, '600', 34, '"Geist Mono", monospace'); x.fillText((d.host || 'demo.sohana.app'), 280, H - 122);
    x.fillStyle = C.ink3; font(x, '500', 24); x.fillText('Demo · simulated money · not a real balance', 280, H - 78);
  }

  function heading(x, eyebrow, y) {
    x.fillStyle = C.mint; font(x, '600', 26, '"Geist Mono", monospace'); x.fillText(eyebrow.toUpperCase().split('').join(String.fromCharCode(8202)), 72, y);
  }

  function drawBalance(x, d) {
    heading(x, d.name + "'s wallet", 260);
    x.fillStyle = C.ink2; font(x, '500', 36); x.fillText('One wallet. ' + d.count + (d.count === 1 ? ' currency.' : ' currencies.'), 72, 320);
    var y = 390, bals = d.balances || [];
    bals.forEach(function (b, i) {
      x.fillStyle = i === 0 ? '#16231A' : C.bg2; rr(x, 72, y, W - 144, 170, 28); x.fill();
      x.strokeStyle = i === 0 ? 'rgba(158,228,147,.45)' : C.line; x.lineWidth = 2; rr(x, 72, y, W - 144, 170, 28); x.stroke();
      x.fillStyle = C.ink3; font(x, '600', 24, '"Geist Mono", monospace'); x.fillText(b.currency, 112, y + 54);
      x.fillStyle = C.ink; var s = fit(x, b.amount, W - 240, '600', i === 0 ? 76 : 60); x.fillText(b.amount, 112, y + 136);
      y += 192;
    });
    if (!bals.length) { x.fillStyle = C.ink2; font(x, '500', 40); x.fillText('Balances loading…', 72, 460); }
    x.fillStyle = C.ink2; font(x, '500', 32); x.fillText('≈ ' + d.total + ' in total · ' + (d.hanatag || ''), 72, Math.min(y + 40, H - 320));
  }

  function drawScore(x, d) {
    heading(x, d.name + "'s credit score", 260);
    var cx = W / 2, cy = 640, r = 250, a0 = Math.PI * 0.8, a1 = Math.PI * 2.2, pct = Math.max(0, Math.min(1, (d.score - 300) / 550));
    x.lineCap = 'round'; x.lineWidth = 40;
    x.strokeStyle = C.bg3; x.beginPath(); x.arc(cx, cy, r, a0, a1); x.stroke();
    x.strokeStyle = C.mint; x.beginPath(); x.arc(cx, cy, r, a0, a0 + (a1 - a0) * Math.max(pct, 0.02)); x.stroke();
    x.textAlign = 'center'; x.fillStyle = C.ink; font(x, '600', 190); x.fillText(String(d.score), cx, cy + 40);
    x.fillStyle = C.ink3; font(x, '500', 32); x.fillText('out of 850 · ' + d.tier, cx, cy + 110);
    if (d.delta > 0) {
      var t = '+' + d.delta + ' today'; font(x, '600', 36); var w = x.measureText(t).width + 56;
      x.fillStyle = 'rgba(158,228,147,.15)'; rr(x, cx - w / 2, cy + 160, w, 66, 33); x.fill();
      x.fillStyle = C.mint; x.fillText(t, cx, cy + 205);
    }
    x.fillStyle = C.ink2; font(x, '500', 32); x.fillText('Njangi Credit Score: built from how I save', cx, 1000);
    x.fillText('with my circle, not from a bank file.', cx, 1044);
    x.textAlign = 'left';
  }

  function drawReceipt(x, d) {
    heading(x, 'Receipt', 260);
    x.fillStyle = C.bg2; rr(x, 72, 300, W - 144, 640, 36); x.fill();
    x.strokeStyle = C.line; x.lineWidth = 2; rr(x, 72, 300, W - 144, 640, 36); x.stroke();
    // check badge
    x.fillStyle = C.mint; x.beginPath(); x.arc(160, 400, 44, 0, Math.PI * 2); x.fill();
    x.strokeStyle = C.bg; x.lineWidth = 9; x.lineCap = 'round'; x.lineJoin = 'round'; x.beginPath(); x.moveTo(140, 401); x.lineTo(155, 416); x.lineTo(182, 386); x.stroke();
    x.fillStyle = C.ink2; font(x, '600', 36); x.fillText(d.title, 230, 412);
    x.fillStyle = d.incoming ? C.mint : C.ink; fit(x, (d.incoming ? '+' : '') + d.amount, W - 260, '600', 120);
    x.fillText((d.incoming ? '+' : '') + d.amount, 120, 580);
    x.fillStyle = C.ink2; font(x, '500', 36);
    var who = d.who || ''; if (who.length > 46) who = who.slice(0, 45) + '…'; x.fillText(who, 120, 650);
    x.strokeStyle = C.line; x.setLineDash([10, 10]); x.lineWidth = 2; x.beginPath(); x.moveTo(120, 720); x.lineTo(W - 120, 720); x.stroke(); x.setLineDash([]);
    font(x, '500', 28, '"Geist Mono", monospace');
    [['By', d.name + (d.hanatag ? '  ' + d.hanatag : '')], ['When', (d.when || '') + ' UTC'], ['Reference', d.ref || ''], ['Status', 'Completed in seconds']].forEach(function (p, i) {
      x.fillStyle = C.ink3; x.fillText(p[0].toUpperCase(), 120, 780 + i * 46);
      x.fillStyle = C.ink; x.textAlign = 'right'; x.fillText(p[1], W - 120, 780 + i * 46); x.textAlign = 'left';
    });
  }

  function render(d) {
    var cv = document.createElement('canvas'); cv.width = W; cv.height = H;
    var x = cv.getContext('2d'); frame(x, d);
    if (d.kind === 'score') drawScore(x, d); else if (d.kind === 'receipt') drawReceipt(x, d); else drawBalance(x, d);
    return cv;
  }

  /* ---------- sheet ---------- */
  var bg = null;
  function close() { if (!bg) return; bg.classList.remove('on'); var b = bg; bg = null; setTimeout(function () { b.remove(); }, 220); }
  function open(html) {
    close();
    bg = document.createElement('div'); bg.className = 'dzs-bg';
    bg.innerHTML = '<div class="dzs" role="dialog" aria-label="Share">' + html + '</div>';
    bg.addEventListener('click', function (e) { if (e.target === bg) close(); });
    document.body.appendChild(bg); requestAnimationFrame(function () { bg && bg.classList.add('on'); });
    return bg;
  }
  function log(kind, channel) { try { fetch('/api/demo/share-log', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: kind, channel: channel }) }); } catch (e) {} }

  function chooser() {
    var b = open('<button class="dzs-x" aria-label="Close">×</button><h3>Share your SOHANA moment</h3><p class="sub">Make a card for Instagram, WhatsApp, X or LinkedIn. It is clearly marked as a demo.</p>' +
      '<div class="dzs-pick"><button data-k="balance">' + svg('wallet') + 'My wallet</button><button data-k="score">' + svg('score') + 'Credit score</button><button data-k="receipt">' + svg('receipt') + 'Last payment</button></div>');
    b.querySelector('.dzs-x').onclick = close;
    b.querySelectorAll('[data-k]').forEach(function (btn) { btn.onclick = function () { share(btn.getAttribute('data-k')); }; });
  }

  function share(kind, tx) {
    var b = open('<button class="dzs-x" aria-label="Close">×</button><h3>Preparing your card…</h3><div class="dzs-prev"><span style="color:#868B84;font-size:14px">One moment</span></div>');
    b.querySelector('.dzs-x').onclick = close;
    var url = '/api/demo/share-data?type=' + encodeURIComponent(kind) + (tx ? '&tx=' + encodeURIComponent(tx) : '');
    var ready = (document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve();
    Promise.all([fetch(url, { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }), ready]).then(function (res) {
      var d = res[0];
      if (!d || d.error) { b.querySelector('h3').textContent = d && d.error === 'none' ? 'No payments yet. Send or receive money first.' : 'Could not make the card.'; return; }
      var cv = render(d), dataUrl = cv.toDataURL('image/png');
      var text = d.text || '', link = d.link || location.origin;
      var enc = encodeURIComponent;
      var titles = { balance: 'Your wallet card', score: 'Your score card', receipt: 'Your receipt' };
      var canNative = !!(navigator.share);
      b.querySelector('.dzs').innerHTML =
        '<button class="dzs-x" aria-label="Close">×</button><h3>' + (titles[d.kind] || 'Your card') + '</h3><p class="sub">Post it and tag @SOHANA. Friends who join from your link show up on the booth screen.</p>' +
        '<div class="dzs-prev"><img alt="Share card preview" src="' + dataUrl + '"></div>' +
        '<div class="dzs-row">' +
          (canNative ? '<button class="dzs-b p" data-a="native">' + svg('share') + 'Share</button>' : '') +
          '<a class="dzs-b' + (canNative ? '' : ' p') + '" data-a="save" download="sohana-' + d.kind + '.png" href="' + dataUrl + '">Save image</a>' +
          (canNative ? '' : '<button class="dzs-b" data-a="copy">Copy text + link</button>') +
        '</div>' +
        '<div class="dzs-row four">' +
          '<a class="dzs-b sm" data-a="whatsapp" target="_blank" rel="noopener" href="https://wa.me/?text=' + enc(text) + '">WhatsApp</a>' +
          '<a class="dzs-b sm" data-a="x" target="_blank" rel="noopener" href="https://twitter.com/intent/tweet?text=' + enc(text) + '">X</a>' +
          '<a class="dzs-b sm" data-a="linkedin" target="_blank" rel="noopener" href="https://www.linkedin.com/sharing/share-offsite/?url=' + enc(link) + '">LinkedIn</a>' +
          '<a class="dzs-b sm" data-a="facebook" target="_blank" rel="noopener" href="https://www.facebook.com/sharer/sharer.php?u=' + enc(link) + '">Facebook</a>' +
        '</div>' +
        '<div class="dzs-cap">' + text.replace(/[<>&]/g, '') + '</div>';
      var box = b.querySelector('.dzs');
      box.querySelector('.dzs-x').onclick = close;
      box.querySelectorAll('[data-a]').forEach(function (el) {
        var a = el.getAttribute('data-a');
        el.addEventListener('click', function (ev) {
          log(d.kind, a);
          if (a === 'native') {
            ev.preventDefault();
            cv.toBlob(function (blob) {
              var file = new File([blob], 'sohana-' + d.kind + '.png', { type: 'image/png' });
              var payload = { title: 'SOHANA', text: text };
              if (navigator.canShare && navigator.canShare({ files: [file] })) payload.files = [file]; else payload.url = link;
              navigator.share(payload).catch(function () {});
            }, 'image/png');
          } else if (a === 'copy') {
            ev.preventDefault();
            (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(function () { el.textContent = 'Copied'; }, function () { el.textContent = 'Copy failed'; });
          }
        });
      });
    }).catch(function () { if (b && b.querySelector('h3')) b.querySelector('h3').textContent = 'Could not make the card.'; });
  }

  window.dzShare = function (kind, tx) { if (!kind) chooser(); else share(kind, tx); };

  /* ---------- floating button on the money pages ---------- */
  var PAGES = /^\/(dashboard|wallet|ncs|history|passport|profile|circles\/[^/]+)$/;
  function addFab() {
    if (!PAGES.test(location.pathname) || document.querySelector('.dzs-fab')) return;
    var b = document.createElement('button'); b.className = 'dzs-fab'; b.type = 'button';
    b.innerHTML = svg('share') + 'Share'; b.onclick = function () { chooser(); };
    document.body.appendChild(b);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addFab); else addFab();

  /* ---------- after a successful payment: offer the receipt ---------- */
  var nudgeEl = null;
  function nudge(label) {
    if (nudgeEl) nudgeEl.remove();
    nudgeEl = document.createElement('div'); nudgeEl.className = 'dzs-nudge';
    nudgeEl.innerHTML = '<span>' + label + '</span><button type="button">Share receipt</button>';
    nudgeEl.querySelector('button').onclick = function () { var n = nudgeEl; nudgeEl = null; n.remove(); share('receipt'); };
    document.body.appendChild(nudgeEl);
    requestAnimationFrame(function () { nudgeEl && nudgeEl.classList.add('on'); });
    var me = nudgeEl; setTimeout(function () { if (me && me.parentNode) { me.classList.remove('on'); setTimeout(function () { me.remove(); }, 500); } }, 9000);
  }
  var WATCH = [
    [/\/api\/wallet\/pay\/confirm$/, 'Payment sent'],
    [/\/api\/rosca\/[^/]+\/contribute$/, 'Contribution paid'],
    [/\/api\/wallet\/convert$/, 'Converted'],
    [/\/api\/campaigns\/[^/]+\/donate$/, 'Donation sent'],
    [/\/api\/wallet\/withdraw$/, 'Cash-out done']
  ];
  // the page may reload right after paying (circle page does): show the offer after the reload
  try {
    var pend = JSON.parse(sessionStorage.getItem('dz_nudge') || 'null');
    sessionStorage.removeItem('dz_nudge');
    if (pend && Date.now() - pend.t < 15000) setTimeout(function () { nudge(pend.l); }, 900);
  } catch (e) {}
  if (window.fetch) {
    var orig = window.fetch;
    window.fetch = function (input, init) {
      var u = typeof input === 'string' ? input : (input && input.url) || '';
      var m = (init && init.method || 'GET').toUpperCase();
      var p = orig.apply(this, arguments);
      if (m === 'POST') {
        var path = u.replace(location.origin, '').split('?')[0], hit = null;
        WATCH.forEach(function (w) { if (w[0].test(path)) hit = w[1]; });
        if (hit) p.then(function (r) { if (r.ok) r.clone().json().then(function (j) {
          if (j && j.ok) {
            try { sessionStorage.setItem('dz_nudge', JSON.stringify({ l: hit, t: Date.now() })); } catch (e) {}
            setTimeout(function () { try { sessionStorage.removeItem('dz_nudge'); } catch (e) {} nudge(hit); }, 1600);
          }
        }).catch(function () {}); });
      }
      return p;
    };
  }
})();
