'use strict';
/* Core: state, helpers, API client, shell, router, charts. */
const App = { me: null, branch: null, page: 'home', timer: null };
const Pages = {};
const Acts = {};

const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const peso = n => '₱' + Number(n || 0).toLocaleString('en-PH', { maximumFractionDigits: 0 });
const num = (n, d = 1) => Number(n).toLocaleString('en-PH', { maximumFractionDigits: d });
const D = iso => new Date(iso + 'T00:00:00');
const pad = n => String(n).padStart(2, '0');
const isoOf = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const addDays = (iso, n) => { const d = D(iso); d.setDate(d.getDate() + n); return isoOf(d); };
const fmtDay = (iso, long) => D(iso).toLocaleDateString('en-PH', long ? { weekday: 'long', month: 'long', day: 'numeric' } : { weekday: 'short', month: 'short', day: 'numeric' });
const isManager = () => App.me && ['manager', 'owner'].includes(App.me.user.role);
const isOwner = () => App.me && App.me.user.role === 'owner';
const chip = (tier, label) => tier ? `<span class="chip ${esc(tier)}">${esc(label)}</span>` : '';
const md = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');   // safe: escape first, then allow bold

let toastTimer;
function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ''; }, isErr ? 5000 : 2600);
}

async function api(path, { method = 'GET', body, form } = {}) {
  let url = path;
  if (App.branch && isOwner() && path.startsWith('/api/') && !path.includes('branch=')) url += (url.includes('?') ? '&' : '?') + 'branch=' + App.branch;
  const opts = { method, headers: { 'X-Requested-With': 'mapo' }, credentials: 'same-origin' };
  if (form) opts.body = form;
  else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opts); }
  catch { const e = new Error("You're offline. Check your connection and try again."); e.offline = true; throw e; }
  if (res.status === 401 && !path.includes('/auth/')) { App.me = null; loginView('Your session ended. Please sign in again.'); throw new Error('Please sign in.'); }
  let data = null;
  try { data = await res.json(); } catch { /* not json */ }
  if (!res.ok) throw new Error((data && data.error) || 'Something went wrong. Try again.');
  return data;
}

/* ---------- icons ---------- */
const ICON = {
  home: 'M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10',
  book: 'M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3V4zM5 17a3 3 0 013-3h11',
  chart: 'M4 20V4M4 20h16M8 16l4-5 3 3 5-7',
  cup: 'M6 4h12l-2 16H8L6 4zM6.5 9h11',
  spark: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18',
  doc: 'M7 3h7l4 4v14H7V3zM14 3v4h4M10 13h5M10 17h5',
  chat: 'M4 5h16v11H9l-5 4V5z',
  gear: 'M12 15a3 3 0 100-6 3 3 0 000 6zM19 12a7 7 0 00-.1-1.2l2-1.5-2-3.4-2.3 1a7 7 0 00-2-1.2L14.2 3h-4l-.4 2.7a7 7 0 00-2 1.2l-2.3-1-2 3.4 2 1.5A7 7 0 005 12c0 .4 0 .8.1 1.2l-2 1.5 2 3.4 2.3-1a7 7 0 002 1.2l.4 2.7h4l.4-2.7a7 7 0 002-1.2l2.3 1 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
};
const icon = n => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON[n]}"/></svg>`;

const NAV = [
  { id: 'home', label: 'Home', icon: 'home', tab: true },
  { id: 'logbook', label: 'Logbook', icon: 'book', tab: true },
  { id: 'forecast', label: 'Forecast', icon: 'chart', tab: true },
  { id: 'stock', label: 'Stock', icon: 'cup', tab: true },
  { id: 'mapo', label: 'MAPO', icon: 'chat' },
  { id: 'insights', label: 'Insights', icon: 'spark', mgr: true },
  { id: 'reports', label: 'Reports', icon: 'doc', mgr: true },
  { id: 'settings', label: 'Settings', icon: 'gear', mgr: true },
];
const navFor = () => NAV.filter(n => !n.mgr || isManager());

/* ---------- views ---------- */
function loginView(err) {
  $('#app').innerHTML = `
  <div class="login"><form class="box" id="loginForm">
    <div class="brand"><span>Mad Potato</span></div>
    <p class="muted" style="margin:6px 0 18px">Sign in to log sales, check stock and see the forecast.</p>
    ${err ? `<div class="banner warn" style="margin-bottom:12px" role="alert">${esc(err)}</div>` : ''}
    <div class="field"><label class="f" for="lu">Username</label><input id="lu" type="text" autocomplete="username" autocapitalize="none" required></div>
    <div class="field"><label class="f" for="lp">Password</label><input id="lp" type="password" autocomplete="current-password" required></div>
    <button class="btn gold" style="width:100%">Sign in</button>
  </form></div>`;
  $('#loginForm').onsubmit = async e => {
    e.preventDefault();
    try {
      await api('/api/auth/login', { method: 'POST', body: { username: $('#lu').value, password: $('#lp').value } });
      await boot();
    } catch (ex) { loginView(ex.message); }
  };
}

function renderShell() {
  const u = App.me.user, nav = navFor();
  const items = nav.map(n => `<button class="nav-btn" data-act="go" data-id="${n.id}" data-nav="${n.id}">${icon(n.icon)}${n.label}</button>`).join('');
  const tabs = nav.filter(n => n.tab).map(n => `<button data-act="go" data-id="${n.id}" data-nav="${n.id}">${icon(n.icon)}${n.label}</button>`).join('')
    + `<button data-act="go" data-id="more" data-nav="more">${icon('more')}More</button>`;
  const branches = App.me.branches;
  const bsel = isOwner() && branches.length > 1
    ? `<select id="branchSel" aria-label="Branch" style="margin:8px 0">${branches.map(b => `<option value="${b.id}" ${b.id == App.branch ? 'selected' : ''}>${esc(b.name)}</option>`).join('')}</select>` : '';
  $('#app').innerHTML = `
  <div class="shell">
    <nav class="rail" aria-label="Main">
      <div class="brand">Mad Potato<small>${esc((branches.find(b => b.id == App.branch) || branches[0] || {}).name || '')}</small></div>
      ${bsel}${items}<div class="spacer"></div>
      <div class="who"><b>${esc(u.display_name)}</b>${esc(u.role)}<br><button class="btn ghost sm" style="margin-top:8px;color:#ddd;border-color:#4a4338" data-act="logout">Sign out</button></div>
    </nav>
    <main class="main" id="main" tabindex="-1"></main>
  </div>
  <nav class="tabbar" aria-label="Main">${tabs}</nav>`;
  const sel = $('#branchSel');
  if (sel) sel.onchange = () => { App.branch = +sel.value; renderShell(); route(); };
}

function markNav() {
  document.querySelectorAll('[data-nav]').forEach(b => {
    const on = b.dataset.nav === App.page || (b.dataset.nav === 'more' && !NAV.some(n => n.tab && n.id === App.page) && App.page !== 'home');
    on ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current');
  });
}

function head(title, sub, actions) {
  return `<div class="topbar"><div><h1>${esc(title)}</h1>${sub ? `<div class="sub">${sub}</div>` : ''}</div><div class="row">${actions || ''}</div></div>`;
}

async function route() {
  if (!App.me) return;
  clearTimeout(App.timer);
  const id = (location.hash.replace(/^#\/?/, '') || 'home').split('?')[0];
  const known = id === 'more' || navFor().some(n => n.id === id);
  App.page = known ? id : 'home';
  markNav();
  const main = $('#main');
  if (!main) return;
  main.oninput = null; main.onchange = null; main.onsubmit = null;
  main.innerHTML = '<div class="empty">Loading…</div>';
  try { await Pages[App.page](main); }
  catch (e) {
    main.innerHTML = `<div class="card"><h2>Couldn't load this page</h2><p class="muted">${esc(e.message)}</p><button class="btn" data-act="reload">Try again</button></div>`;
  }
  window.scrollTo(0, 0);
}

/* re-run the page after a delay while the heavy forecast models finish */
function pollWhile(cond, ms = 7000) {
  clearTimeout(App.timer);
  const page = App.page;
  let n = (pollWhile.n = (pollWhile.page === page ? pollWhile.n : 0));
  pollWhile.page = page;
  if (cond && n < 12) { pollWhile.n++; App.timer = setTimeout(() => { if (App.page === page) Pages[page]($('#main')); }, ms); }
}

/* ---------- global events ---------- */
document.addEventListener('click', async e => {
  const el = e.target.closest('[data-act]');
  if (!el || !Acts[el.dataset.act]) return;
  try { await Acts[el.dataset.act](el, e); } catch (ex) { toast(ex.message, true); }
});
Acts.go = el => { location.hash = '#/' + el.dataset.id; };
Acts.reload = () => route();
Acts.logout = async () => { await api('/api/auth/logout', { method: 'POST' }); App.me = null; loginView(); };
window.addEventListener('hashchange', route);

/* ---------- charts (plain SVG, no libraries, work offline) ---------- */
function niceMax(v) { const p = Math.pow(10, Math.floor(Math.log10(v || 1))); return Math.ceil(v / p * 2) / 2 * p || 1; }

function lineChart({ history = [], forecast = [], ref }) {
  const pts = [...history.map(h => ({ d: h.date, v: h.sales, k: 'h' })), ...forecast.map(f => ({ d: f.date, v: f.yhat, lo: f.lower, hi: f.upper, k: 'f', f }))];
  if (pts.length < 2) return '<div class="empty">Not enough data to draw a chart yet.</div>';
  const W = 720, H = 270, L = 46, R = 12, T = 14, B = 28;
  const max = niceMax(Math.max(...pts.map(p => p.hi || p.v), ref || 0) * 1.05);
  const x = i => L + (W - L - R) * i / (pts.length - 1);
  const y = v => T + (H - T - B) * (1 - v / max);
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Sales history and forecast">`;
  for (let k = 0; k <= 4; k++) { const v = max * k / 4; s += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v >= 1000 ? num(v / 1000, 1) + 'k' : num(v, 0)}</text>`; }
  if (ref) s += `<line class="refline" x1="${L}" x2="${W - R}" y1="${y(ref)}" y2="${y(ref)}"/>`;
  const idx = pts.map((p, i) => i);
  const hi = idx.filter(i => pts[i].k === 'h'), fi = idx.filter(i => pts[i].k === 'f');
  const path = arr => arr.map((i, j) => `${j ? 'L' : 'M'}${x(i).toFixed(1)},${y(pts[i].v).toFixed(1)}`).join('');
  if (fi.length) {
    const start = hi.length ? [hi[hi.length - 1], ...fi] : fi;
    s += `<path class="band" d="${fi.map((i, j) => `${j ? 'L' : 'M'}${x(i)},${y(pts[i].hi)}`).join('')}${[...fi].reverse().map(i => `L${x(i)},${y(pts[i].lo)}`).join('')}Z"/>`;
    s += `<path class="fc" d="${path(start)}"/>`;
    fi.forEach(i => { const f = pts[i].f; if (f && (f.holiday || f.payday || f.event)) s += `<circle cx="${x(i)}" cy="${y(pts[i].v)}" r="4" fill="${f.holiday ? '#d6382f' : f.event ? '#1b6a8a' : '#2f7d4f'}"><title>${esc(fmtDay(f.date))}: ${esc(f.holiday || (f.event ? 'Event' : 'Payday'))}</title></circle>`; });
  }
  if (hi.length) s += `<path class="hist" d="${path(hi)}"/>`;
  const step = Math.max(1, Math.ceil(pts.length / 7));
  pts.forEach((p, i) => { if (i % step === 0) s += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(D(p.d).toLocaleDateString('en-PH', { month: 'short', day: 'numeric' }))}</text>`; });
  return s + '</svg><div class="legend"><span><i style="background:#123d27"></i>Actual</span><span><i style="background:#7a5800"></i>Forecast</span><span><i style="background:#f4b223;opacity:.5"></i>Likely range</span><span><i style="background:#2f7d4f"></i>Payday</span><span><i style="background:#d6382f"></i>Holiday</span><span><i style="background:#1b6a8a"></i>Your event</span></div>';
}

const TIER_COLOR = { deficit: '#b3261e', bad: '#d9741a', 'below-avg': '#b58a00', acceptable: '#2f7d4f', successful: '#1b6a8a' };
function tierBars(rows) {
  if (!rows.length) return '<div class="empty">No sales logged yet.</div>';
  const W = 600, H = 150, B = 22, max = niceMax(Math.max(...rows.map(r => r.sales)) * 1.05), bw = (W - 10) / rows.length;
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily sales, last ${rows.length} days">`;
  rows.forEach((r, i) => {
    const h = (H - B - 6) * r.sales / max;
    s += `<rect x="${5 + i * bw + 3}" y="${H - B - h}" width="${bw - 6}" height="${h}" rx="3" fill="${TIER_COLOR[r.tier] || '#5b6270'}"><title>${esc(fmtDay(r.date))}: ${peso(r.sales)} (${esc(r.tier_label)})</title></rect>`;
    s += `<text x="${5 + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle">${D(r.date).getDate()}</text>`;
  });
  return s + '</svg>';
}

function weekdayBars(rows, best) {
  const W = 520, H = 190, B = 24, max = niceMax(Math.max(...rows.map(r => r.avg), 1) * 1.1), bw = W / 7;
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Average sales by weekday">`;
  rows.forEach((r, i) => {
    const h = (H - B - 16) * r.avg / max, cls = r.day === best ? 'bar best' : 'bar';
    s += `<rect class="${cls}" x="${i * bw + 10}" y="${H - B - h}" width="${bw - 20}" height="${h}" rx="4"/>`;
    s += `<text x="${i * bw + bw / 2}" y="${H - B - h - 5}" text-anchor="middle">${r.days ? peso(r.avg) : ''}</text><text x="${i * bw + bw / 2}" y="${H - 7}" text-anchor="middle">${r.day.slice(0, 3)}</text>`;
  });
  return s + '</svg>';
}

function cups(rows) {
  if (!rows.length) return '<div class="empty">No stock counts yet. Log today\'s stock to see your cups fill up.</div>';
  return `<div class="cups">${rows.map(r => {
    const full = Math.max(r.reorder_point || 0, (r.avg_daily || 0) * (r.lead_days + 7), r.stock) || 1;
    const pct = r.status === 'unknown' ? 50 : Math.min(100, Math.max(4, r.stock / full * 100));
    return `<div class="cupcell ${r.status}" title="${esc(r.name)}"><div class="cup"><div class="fill" style="height:${pct}%"></div></div>
      <div class="code">${esc(r.code)}</div><div class="qty">${num(r.stock, 0)} ${esc(r.unit)}</div>
      <div class="qty">${r.days_cover != null ? num(r.days_cover) + ' days' : ''}</div></div>`;
  }).join('')}</div>`;
}

/* ---------- boot ---------- */
async function boot() {
  try {
    App.me = await api('/api/auth/me');
    if (App.branch == null || !App.me.branches.some(b => b.id == App.branch)) App.branch = App.me.user.branch_id || (App.me.branches[0] || {}).id;
    renderShell();
    flushQueue();
    route();
  } catch { loginView(); }
}

if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => { }));
window.addEventListener('online', () => { if (App.me) flushQueue(); });
document.addEventListener('DOMContentLoaded', boot);
