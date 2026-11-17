/*
 * SOHANA demo UI layer — loaded only on the demo service (DEMO_MODE=1).
 *  - line icons instead of emoji and glyph icons
 *  - plain-language labels ("Send", "Add money", "circle" instead of "ROSCA")
 *  - wallet limits & fees folded into one "Limits & fees" panel
 *  - "Demo" badge in the top bar
 *  - in-app notification banner when money or circle activity arrives
 */
(function () {
  "use strict";

  /* ---------------- icons (24px line set, 1.8 stroke) ---------------- */
  var P = {
    home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    wallet: '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M3 10h18"/><circle cx="16.5" cy="15" r="1.2" fill="currentColor"/>',
    circles: '<circle cx="12" cy="12" r="8" stroke-dasharray="2.5 2.5"/><circle cx="12" cy="4" r="2" fill="currentColor"/><circle cx="19" cy="15" r="2"/><circle cx="5" cy="15" r="2"/>',
    score: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4.5-5.5" stroke-width="2.2"/><circle cx="12" cy="17" r="1.4" fill="currentColor"/>',
    more: '<circle cx="5" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="19" cy="12" r="1.6" fill="currentColor"/>',
    history: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    news: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h10M7 12h10M7 16h6"/>',
    heart: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/>',
    users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17" cy="9" r="2.6"/><path d="M15.5 14.2A5 5 0 0 1 21 19"/>',
    coins: '<ellipse cx="12" cy="6.5" rx="7" ry="3"/><path d="M5 6.5v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5"/><path d="M5 11.5v5c0 1.7 3.1 3 7 3s7-1.3 7-3v-5"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    award: '<circle cx="12" cy="9" r="5.5"/><path d="M8.5 13.5 7 21l5-2.5 5 2.5-1.5-7.5"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    sprout: '<path d="M12 21v-8"/><path d="M12 13c0-4 3-7 8-7 0 4-3 7-8 7z"/><path d="M12 15c0-3-2.4-5.5-6.5-5.5 0 3 2.4 5.5 6.5 5.5z"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
    lock: '<rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    pin: '<path d="M12 21s-6-5.3-6-11a6 6 0 0 1 12 0c0 5.7-6 11-6 11z"/><circle cx="12" cy="10" r="2.2"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    sparkles: '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
    alert: '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17v.5"/>',
    bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    card: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3 10h18M7 15h4"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
    phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
    gift: '<rect x="3" y="9" width="18" height="12" rx="1.5"/><path d="M3 13h18M12 9v12"/><path d="M12 9c-1.5-3-5-4-5-1.5S10 9 12 9zM12 9c1.5-3 5-4 5-1.5S14 9 12 9z"/>',
    bank: '<path d="M3 9.5 12 4l9 5.5"/><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18"/>',
    send: '<path d="M4 12 20 4l-6.5 16-2.5-6.5z"/><path d="M11 13.5 20 4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    convert: '<path d="M4 8h13l-3-3M20 16H7l3 3"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8"/>',
    chat: '<path d="M4 5h16v11H9l-5 4z"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    briefcase: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5h6v2M3 12h18"/>',
    out: '<path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/>',
    star: '<path d="M12 3.5l2.6 5.3 5.9.9-4.2 4.1 1 5.8L12 16.9l-5.3 2.7 1-5.8-4.2-4.1 5.9-.9z"/>'
  };
  function svg(name) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (P[name] || P.sparkles) + "</svg>";
  }

  /* ---------------- emoji → icon ---------------- */
  var EMOJI = {
    "🏘": "home", "🏠": "home", "🏡": "home", "👥": "users", "👤": "user", "💰": "coins", "💵": "coins", "💶": "coins", "💸": "coins",
    "📊": "chart", "📈": "chart", "📉": "chart", "🛡": "shield", "📰": "news", "📝": "file", "💚": "heart", "❤": "heart", "🤝": "users",
    "🏦": "bank", "🎉": "sparkles", "🥳": "sparkles", "✨": "sparkles", "✅": "check", "✔": "check", "☑": "check", "🌱": "sprout",
    "🔗": "link", "📄": "file", "📋": "file", "🔒": "lock", "🔐": "lock", "📍": "pin", "📅": "calendar", "🗓": "calendar",
    "🏆": "award", "🥇": "award", "🎖": "award", "🏅": "award", "⚠": "alert", "🔔": "bell", "💳": "card", "🌍": "globe", "🌎": "globe",
    "🌐": "globe", "📱": "phone", "🎁": "gift", "⭐": "star", "🌟": "star", "💬": "chat", "📧": "mail", "✉": "mail", "💼": "briefcase",
    "🎗": "heart", "🙏": "heart", "🔁": "convert", "🔄": "convert", "↻": "convert", "📤": "send", "📥": "plus", "🚀": "sparkles", "🟢": "check"
  };
  var EMOJI_RE = /\p{Extended_Pictographic}(?:️|‍\p{Extended_Pictographic}|[\u{1F3FB}-\u{1F3FF}])*/gu;
  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, SELECT: 1, OPTION: 1, CODE: 1, PRE: 1, NOSCRIPT: 1 };

  function swapEmoji(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentNode;
        if (!p || SKIP[p.nodeName] || (p.closest && p.closest("[data-dz-keep],#demo-banner,.dz-toast"))) return NodeFilter.FILTER_REJECT;
        EMOJI_RE.lastIndex = 0;
        return EMOJI_RE.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      }
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (n) {
      var text = n.nodeValue, frag = document.createDocumentFragment(), last = 0, m;
      EMOJI_RE.lastIndex = 0;
      while ((m = EMOJI_RE.exec(text))) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var base = m[0].replace(/[️‍]|[\u{1F3FB}-\u{1F3FF}]/gu, "");
        var name = EMOJI[base] || EMOJI[Array.from(base)[0]];
        if (name) {
          var s = document.createElement("span");
          s.className = "dz-ico";
          s.innerHTML = svg(name);
          frag.appendChild(s);
        }
        last = m.index + m[0].length;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last).replace(/^\s+(?=\S)/, last ? " " : "")));
      n.parentNode.replaceChild(frag, n);
    });
  }

  /* ---------------- navigation icons by destination ---------------- */
  var NAV = [
    ["/dashboard", "home"], ["/wallet", "wallet"], ["/circles", "circles"], ["/ncs", "score"], ["/history", "history"],
    ["/kyc", "shield"], ["/blog", "news"], ["/campaigns", "heart"], ["/my-campaigns", "heart"], ["/passport", "card"],
    ["/profile", "user"], ["/logout", "out"], ["/notifications", "bell"], ["/admin", "chart"]
  ];
  function navIcon(href) {
    for (var i = 0; i < NAV.length; i++) if (href && href.indexOf(NAV[i][0]) === 0) return NAV[i][1];
    return null;
  }
  function swapNav() {
    document.querySelectorAll(".sidebar a .nav-icon, .sidebar-bottom a .nav-icon").forEach(function (el) {
      var a = el.closest("a"), name = navIcon(a && a.getAttribute("href"));
      if (name) el.innerHTML = svg(name);
    });
    document.querySelectorAll(".bottom-nav .bottom-nav-item").forEach(function (a) {
      var href = a.getAttribute("href") || "", name = navIcon(href) || (/more/i.test(a.textContent) ? "more" : null);
      if (!name) return;
      var ico = a.querySelector(".bn-icon, span, i");
      if (ico && ico.textContent.trim().length <= 2) { ico.innerHTML = svg(name); ico.classList.add("bn-icon"); }
    });
  }

  /* ---------------- plain-language labels ---------------- */
  var EXACT = {
    "My ROSCAs": "Circles", "Dashboard": "Home", "+ Create ROSCA": "+ New circle", "Create ROSCA": "New circle",
    "Deposit": "Add money", "Pay": "Send", "NCS Score": "Credit score", "NCS": "Score"
  };
  function relabel(root) {
    root.querySelectorAll("a, button, .topbar-title, .bottom-nav-item span, .sidebar-nav a, h1, h2, h3, .cd-tab, label").forEach(function (el) {
      // only leaf-ish elements whose whole text is the label
      for (var i = 0; i < el.childNodes.length; i++) {
        var n = el.childNodes[i];
        if (n.nodeType !== 3) continue;
        var t = n.nodeValue.trim();
        if (EXACT[t] && !(t === "NCS" && !el.closest(".bottom-nav"))) n.nodeValue = n.nodeValue.replace(t, EXACT[t]);
      }
    });
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = walker.nextNode())) {
      var p = n.parentNode;
      if (!p || SKIP[p.nodeName]) continue;
      var v = n.nodeValue;
      if (/ROSCA/.test(v)) n.nodeValue = v.replace(/^(\s*)ROSCAs\b/, "$1Circles").replace(/^(\s*)ROSCA\b/, "$1Circle")
        .replace(/\bROSCAs\b/g, "circles").replace(/\bROSCA\b/g, "circle");
      if (/@@/.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(/@@/g, "@");
    }
  }

  /* ---------------- wallet: one "Limits & fees" panel ---------------- */
  function foldLimits() {
    var limits = document.querySelector(".w-grid3"), fees = document.querySelector(".w-fees");
    if (!limits && !fees) return;
    var d = document.createElement("details");
    d.className = "dz-limits";
    d.innerHTML = "<summary>Limits &amp; fees</summary><div class='dz-limits-body'></div>";
    var anchor = limits || fees;
    anchor.parentNode.insertBefore(d, anchor);
    var body = d.querySelector(".dz-limits-body");
    if (limits) body.appendChild(limits);
    if (fees) body.appendChild(fees);
  }

  /* ---------------- "Demo" badge in the top bar ---------------- */
  function badge() {
    var t = document.querySelector(".topbar-title");
    if (!t || t.querySelector(".dz-badge")) return;
    var b = document.createElement("span");
    b.className = "dz-badge";
    b.textContent = "Demo";
    b.title = "Demo environment: simulated money, no real transactions";
    t.appendChild(b);
    var floating = document.getElementById("demo-banner");
    if (floating) floating.style.display = "none";
  }

  /* ---------------- in-app notification banner ---------------- */
  function notifyIcon(title) {
    var s = (title || "").toLowerCase();
    if (/receiv|payment|sent|money|fund|payout|deposit/.test(s)) return "coins";
    if (/circle|rosca|member|added/.test(s)) return "circles";
    if (/score|ncs|tier|badge/.test(s)) return "score";
    if (/campaign|donat/.test(s)) return "heart";
    return "bell";
  }
  window.dzToast = function (title, body) {
    var t = document.createElement("div");
    t.className = "dz-toast";
    t.setAttribute("role", "status");
    t.innerHTML = '<div class="dz-t-ico">' + svg(notifyIcon(title)) + '</div><div><b></b><span></span></div><small>now</small>';
    t.querySelector("b").textContent = title || "";
    t.querySelector("span").textContent = body || "";
    t.addEventListener("click", function () { location.reload(); });
    document.body.appendChild(t);
    requestAnimationFrame(function () { requestAnimationFrame(function () { t.classList.add("show"); }); });
    setTimeout(function () { t.classList.remove("show"); setTimeout(function () { t.remove(); }, 600); }, 5200);
  };

  /* ---------------- big numbers set in Geist Mono -> Geist ---------------- */
  function numbers() {
    document.querySelectorAll(".main-area *").forEach(function (el) {
      if (el.children.length > 2 || el.classList.contains("dz-num")) return;
      var cs = getComputedStyle(el);
      if (cs.fontFamily.indexOf("Mono") !== -1 && parseFloat(cs.fontSize) >= 18 && /[0-9]/.test(el.textContent)) el.classList.add("dz-num");
    });
  }

  /* ---------------- run ---------------- */
  function run() {
    try { swapNav(); } catch (e) {}
    try { relabel(document.body); } catch (e) {}
    try { swapEmoji(document.body); } catch (e) {}
    try { foldLimits(); } catch (e) {}
    try { badge(); } catch (e) {}
    try { numbers(); } catch (e) {}
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();

  // content rendered later by page scripts (lists, modals, toasts)
  var pending = false;
  new MutationObserver(function (muts) {
    if (pending) return;
    for (var i = 0; i < muts.length; i++) {
      if (muts[i].addedNodes.length) {
        pending = true;
        setTimeout(function () {
          pending = false;
          try { relabel(document.body); swapEmoji(document.body); } catch (e) {}
        }, 60);
        return;
      }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
})();
