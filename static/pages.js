'use strict';
/* Day-to-day pages: Home, Logbook, Forecast, Stock, MAPO. */

/* ---------- offline queue for log saves ---------- */
const Q_KEY = 'mapo_queue';
const queueRead = () => { try { return JSON.parse(localStorage.getItem(Q_KEY) || '[]'); } catch { return []; } };
const queueWrite = q => { try { localStorage.setItem(Q_KEY, JSON.stringify(q)); } catch { /* storage full or blocked */ } };
async function flushQueue() {
  const q = queueRead();
  if (!q.length || !navigator.onLine) return;
  const rest = []; let sent = 0, dropped = 0;
  for (const it of q) {
    try { await api(it.path, { method: 'PUT', body: it.body }); sent++; }
    catch (e) { e.offline ? rest.push(it) : dropped++; }
  }
  queueWrite(rest);
  if (sent) toast(`${sent} saved entr${sent > 1 ? 'ies' : 'y'} uploaded.`);
  if (dropped) toast(`${dropped} offline entr${dropped > 1 ? 'ies were' : 'y was'} rejected. Re-enter ${dropped > 1 ? 'them' : 'it'} in the Logbook.`, true);
}

/* ---------- Home ---------- */
Pages.home = async main => {
  const d = await api('/api/dashboard');
  const t = d.today_info, tm = d.tomorrow, s = d.stats;
  const urgent = d.stock.filter(r => r.status === 'critical' || r.status === 'reorder');
  const change = s.change_pct == null ? '' : `<span class="${s.change_pct >= 0 ? 'up' : 'down'}">${s.change_pct >= 0 ? '▲' : '▼'} ${Math.abs(s.change_pct)}%</span> vs the week before`;
  const tiers = Object.entries(d.tier_counts).map(([k, v]) => `<div class="row between"><span>${chip(k, TIER_LABEL[k])}</span><b>${v} day${v > 1 ? 's' : ''}</b></div>`).join('') || '<div class="muted">No days logged in the last 30 days.</div>';
  main.innerHTML = head(`Good ${new Date().getHours() < 12 ? 'morning' : new Date().getHours() < 18 ? 'afternoon' : 'evening'}, ${App.me.user.display_name.split(' ')[0]}`, `${fmtDay(d.today, true)} · ${esc(d.branch.name)}`,
    `<button class="btn gold" data-act="go" data-id="logbook">${t.logged ? 'Edit today' : 'Log today'}</button>`) +
    (!t.logged ? `<div class="banner" style="margin-bottom:14px">Today's sales aren't logged yet. The forecast and stock estimates get better with every day you log.</div>` : '') +
    `<div class="grid g3">
      <div class="card kpi"><div class="label">Today</div><div class="value">${t.logged ? peso(t.sales) : '—'}</div><div class="note">${t.logged ? chip(t.tier, t.tier_label) : 'Not logged yet'}</div></div>
      <div class="card kpi"><div class="label">Tomorrow (forecast)</div><div class="value">${tm ? peso(tm.yhat) : '—'}</div><div class="note">${tm ? `${chip(tm.tier, tm.tier_label)} ${peso(tm.lower)} to ${peso(tm.upper)}` : esc(d.notes[0] || 'Log a few days to start')}</div></div>
      <div class="card kpi"><div class="label">Next ${d.week_days || 7} days (forecast)</div><div class="value">${d.week_total ? peso(d.week_total) : '—'}</div><div class="note">${d.week_total ? peso(d.week_total / d.week_days) + ' per day' : ''}</div></div>
      <div class="card kpi"><div class="label">Average, last ${s.days || 7} logged days</div><div class="value">${s.days ? peso(s.avg) : '—'}</div><div class="note">${change}</div></div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><div class="row between"><h2>Stock on hand</h2>${d.stock_date ? `<span class="small muted">Counted ${fmtDay(d.stock_date)}</span>` : ''}</div>
        ${cups(d.stock)}
        ${urgent.length ? `<div class="banner warn" style="margin-top:12px"><b>${urgent.length} item${urgent.length > 1 ? 's need' : ' needs'} ordering:</b> ${urgent.map(r => esc(r.code)).join(', ')}. <a href="#/stock">See the order list</a></div>` : (d.stock.length ? '<div class="banner ok" style="margin-top:12px">Stock covers the forecast demand.</div>' : '')}
      </div>
      <div class="card"><h2>Last ${d.recent.length || 14} days</h2>${tierBars(d.recent)}<div class="hint">Bar colour shows the sales tier.</div></div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h2>Tiers, last 30 days</h2><div class="stack">${tiers}</div></div>
      ${isManager() ? managerHome(d) : ''}
    </div>
    ${d.model_label ? `<p class="small muted" style="margin-top:14px">Forecast model: ${esc(d.model_label)}${d.refreshing ? '. Comparing deeper models in the background…' : ''}</p>` : ''}`;
  pollWhile(d.refreshing);
};
const TIER_LABEL = { deficit: 'Operating Deficit', bad: 'Underperforming', 'below-avg': 'Below Average', acceptable: 'Acceptable', successful: 'High-Performing' };

function managerHome(d) {
  const a = d.anomalies || [], p = d.profit_week;
  const alerts = a.length ? a.map(x => `<div class="alert"><span class="dot ${x.severity}"></span><div><b>${esc(x.title)}</b><span class="small muted">${fmtDay(x.date)}</span><div class="small">${esc(x.detail)}</div></div></div>`).join('')
    : '<div class="empty">Nothing unusual. Sales and stock look consistent.</div>';
  const profit = d.costs_configured && p.days
    ? `<div class="row between"><span>Gross margin</span><b>${p.margin_pct}%</b></div><div class="row between"><span>After operating cost</span><b class="${p.net >= 0 ? 'up' : 'down'}">${peso(p.net)}</b></div>`
    : `<div class="small muted">Add ingredient costs in <a href="#/settings">Settings</a> to see margin and profit.</div>`;
  return `<div class="card"><h2>Things to check</h2>${alerts}
    <h3 style="margin-top:14px">Profit, last 7 days</h3><div class="stack" style="margin-top:8px">${profit}</div></div>`;
}

/* ---------- Logbook (same flow as the V1 digitized logbook) ----------
   Every item:  Starting - Usage = End
   Cup items:   Opening + Stocks = Starting      Opening - Usage = Closing
   Yellow cells were calculated for you; type over them to change. */
const LB = { items: {}, calc: {}, cups: new Set(), order: [] };
const FIELDS = ['opening', 'stocks', 'starting', 'usage', 'ending', 'closing'];
const r2 = x => Math.round(x * 100) / 100;
const fmtIn = x => (x == null ? '' : String(r2(x)));

/* "12+8" -> 20, "" -> null, "12+" (unfinished) -> undefined */
function parseExpr(s) {
  s = String(s).trim();
  if (s === '') return null;
  if (!/^-?\d*\.?\d+(\s*[+-]\s*\d*\.?\d+)*$/.test(s)) return undefined;
  return r2(s.match(/[+-]?\s*\d*\.?\d+/g).reduce((a, p) => a + parseFloat(p.replace(/\s+/g, '')), 0));
}

function recalc(code, ch) {
  const s = LB.items[code], c = LB.calc[code], cup = LB.cups.has(code);
  if (cup) {
    if ((ch === 'opening' || ch === 'stocks') && s.opening != null && s.stocks != null) { s.starting = r2(s.opening + s.stocks); c.starting = true; }
    else if (ch === 'opening' && s.opening != null && s.starting != null) { s.stocks = r2(s.starting - s.opening); c.stocks = true; }
    if (ch === 'starting' && s.opening != null && s.starting != null) { s.stocks = r2(s.starting - s.opening); c.stocks = true; }
    if (ch === 'closing' && s.opening != null && s.closing != null) { s.usage = r2(s.opening - s.closing); c.usage = true; }
  }
  s.ending = s.starting != null && s.usage != null ? r2(s.starting - s.usage) : null;
  if (cup && ch !== 'closing') s.closing = s.opening != null && s.usage != null ? r2(s.opening - s.usage) : null;
  if (cup && s.closing != null && ch !== 'closing') c.closing = true;
}

function paintRow(code, skip) {
  const s = LB.items[code], c = LB.calc[code];
  document.querySelectorAll(`[data-code="${code}"]`).forEach(el => {
    const f = el.dataset.f;
    if (f === 'ending') {
      el.textContent = s.ending == null ? '–' : num(s.ending, 2);
      el.classList.toggle('neg', s.ending != null && s.ending < 0);
      el.title = s.ending != null && s.ending < 0 ? 'Used more than the starting count' : '';
    } else if (f !== skip) {
      el.value = fmtIn(s[f]);
      el.classList.remove('bad');
    }
    if (f !== 'ending') el.classList.toggle('calc', !!c[f]);
  });
  document.querySelectorAll(`[data-code="${code}"][data-f="usage"],[data-code="${code}"][data-f="closing"]`).forEach(el => {
    const v = s[el.dataset.f];
    el.classList.toggle('bad', v != null && v < 0 && el.dataset.f === 'usage');
    el.classList.toggle('warn', v != null && v < 0 && el.dataset.f === 'closing');
    if (el.dataset.f === 'closing') el.title = v != null && v < 0 ? 'Used more than the opening count. If you prepared more cups, add them to Opening (for example 3+50).' : '';
  });
}

function logInput(code, f, ro) {
  return `<input type="text" inputmode="decimal" autocomplete="off" data-code="${esc(code)}" data-f="${f}" ${ro ? 'readonly' : ''} aria-label="${esc(code)} ${f}">`;
}

Pages.logbook = async main => {
  const date = App.logDate || App.me.today;
  const items = await api('/api/items');
  let log, cached = false;
  try { log = await api(`/api/logs/${date}`); localStorage.setItem(`mapo_tmpl_${App.branch}_${date}`, JSON.stringify(log)); }
  catch (e) {
    if (!e.offline) throw e;
    const c = localStorage.getItem(`mapo_tmpl_${App.branch}_${date}`);
    if (!c) throw e;
    log = JSON.parse(c); cached = true;
  }
  const recent = await api('/api/logs?last=14').catch(() => []);
  if (log.thresholds) App.th = log.thresholds;
  const ro = !log.editable;
  LB.cups = new Set(items.filter(i => i.cup).map(i => i.code));
  LB.order = items.map(i => i.code);
  LB.items = {}; LB.calc = {};
  items.forEach(it => {
    const v = log.items[it.code] || {};
    LB.items[it.code] = { opening: v.opening ?? null, stocks: v.stocks ?? null, starting: v.starting ?? 0, usage: v.usage ?? null, ending: v.ending ?? null, closing: v.closing ?? null };
    LB.calc[it.code] = {};
    if (!log.exists) FIELDS.forEach(f => { if (f !== 'usage' && f !== 'ending' && f !== 'closing' && LB.items[it.code][f] != null) LB.calc[it.code][f] = true; });
  });
  const leftRows = items.map(it => `<tr><td><b>${esc(it.code)}</b></td><td>${logInput(it.code, 'starting', ro)}</td><td>${logInput(it.code, 'usage', ro)}</td><td class="endcell" data-code="${esc(it.code)}" data-f="ending">–</td></tr>`).join('');
  const rightRows = items.filter(i => i.cup).map(it => `<tr><td><b>${esc(it.code)}</b></td><td>${logInput(it.code, 'opening', ro)}</td><td>${logInput(it.code, 'stocks', ro)}</td><td>${logInput(it.code, 'closing', ro)}</td></tr>`).join('');
  const recentRows = recent.slice().reverse().map(r => `<tr data-act="pickDate" data-date="${r.date}" style="cursor:pointer"><td>${fmtDay(r.date)}</td><td class="num">${r.sales > 0 ? peso(r.sales) : '—'}</td><td>${chip(r.tier, r.tier_label)}</td></tr>`).join('');
  main.innerHTML = head('Logbook', fmtDay(date, true), `
      <button class="btn ghost sm" data-act="shiftDate" data-n="-1" aria-label="Previous day">◀</button>
      <input type="date" id="logDate" value="${date}" max="${App.me.today}" style="width:auto" aria-label="Log date">
      <button class="btn ghost sm" data-act="shiftDate" data-n="1" aria-label="Next day" ${date >= App.me.today ? 'disabled' : ''}>▶</button>`) +
    (cached ? '<div class="banner warn" style="margin-bottom:12px">You\'re offline. Showing the last saved copy. New entries will upload when you reconnect.</div>' : '') +
    (ro ? `<div class="banner" style="margin-bottom:12px">${date > App.me.today ? 'This date is in the future.' : 'Staff can edit today and yesterday only. Ask a manager to change older days.'}</div>` : '') +
    (!log.exists ? '<div class="banner" style="margin-bottom:12px">New day. Starting, Opening and Stocks are filled in from yesterday\'s count. Enter the usage when the day is done, and End is worked out for you.</div>' : '') +
    `<div class="logcols">
      <div class="card"><h2>Usage count</h2><p class="hint" style="margin:-4px 0 8px">Starting accepts sums like 12+8. Press Enter to jump to the next item.</p>
        <table class="logtbl"><thead><tr><th>Item</th><th class="num">Starting</th><th class="num">Usage</th><th class="num">End</th></tr></thead><tbody>${leftRows}</tbody></table></div>
      <div class="card"><h2>Cup stock</h2><p class="hint" style="margin:-4px 0 8px">Opening plus stocks gives your starting count.</p>
        <table class="logtbl"><thead><tr><th>Item</th><th class="num">Opening</th><th class="num">Stocks</th><th class="num">Closing</th></tr></thead><tbody>${rightRows}</tbody></table>
        <div class="hint"><span class="swatch"></span> Highlighted values were calculated for you. Type over them to change.</div></div>
    </div>
    <div class="card" style="margin-top:14px"><div class="grid g2" style="gap:14px">
      <div><h2>Day total</h2><div class="field"><label class="f" for="sales">Sales (₱)</label><input id="sales" type="text" inputmode="decimal" value="${log.sales || ''}" ${ro ? 'readonly' : ''}></div>
        <div id="tierNow" style="min-height:26px"></div></div>
      <div><div class="field"><label class="f" for="notes">Notes</label><textarea id="notes" rows="3" maxlength="500" placeholder="Event, weather, promo, anything unusual" ${ro ? 'readonly' : ''}>${esc(log.notes)}</textarea></div>
        ${ro ? '' : '<div class="row"><button class="btn gold" data-act="saveLog">Save day</button>' + (isManager() && log.exists ? '<button class="btn ghost sm" data-act="deleteLog">Delete this day</button>' : '') + '</div>'}</div>
    </div></div>
    <div class="card" style="margin-top:14px"><h2>Recent days</h2><div class="tbl-wrap"><table><thead><tr><th>Date</th><th class="num">Sales</th><th>Tier</th></tr></thead><tbody>${recentRows || '<tr><td colspan="3" class="empty">No days logged yet.</td></tr>'}</tbody></table></div></div>`;

  $('#logDate').onchange = e => { if (e.target.value) { App.logDate = e.target.value; Pages.logbook(main); } };
  LB.order.forEach(code => paintRow(code));
  main.oninput = e => {
    const el = e.target;
    if (el.id === 'sales') { showTier(); return; }
    if (!el.dataset.f) return;
    const v = parseExpr(el.value);
    el.classList.toggle('bad', v === undefined);
    if (v === undefined) return;                       // still typing, e.g. "12+"
    const code = el.dataset.code, f = el.dataset.f;
    LB.items[code][f] = f === 'starting' && v == null ? 0 : v;
    delete LB.calc[code][f];
    recalc(code, f);
    paintRow(code, f);
  };
  main.onchange = e => {                               // leaving the cell turns "12+8" into 20
    const el = e.target;
    if (!el.dataset.f) return;
    const v = parseExpr(el.value);
    if (v !== undefined) el.value = fmtIn(v);
  };
  main.onkeydown = e => {
    if (e.key !== 'Enter' || !e.target.dataset.f) return;
    e.preventDefault();
    const el = e.target, v = parseExpr(el.value);
    if (v !== undefined) el.value = fmtIn(v);
    const col = [...main.querySelectorAll(`input[data-f="${el.dataset.f}"]`)];
    const next = col[col.indexOf(el) + 1];
    if (next) { next.focus(); next.select(); }
  };
  showTier();
};

function showTier() {
  const el = $('#tierNow'), v = parseExpr(($('#sales') || {}).value || '') || 0, th = App.th;
  if (!el || !th) return;
  const k = v <= 0 ? null : v < th.deficit ? 'deficit' : v < th.under ? 'bad' : v < th.below ? 'below-avg' : v <= th.high ? 'acceptable' : 'successful';
  el.innerHTML = k ? chip(k, TIER_LABEL[k]) : '';
}
Acts.pickDate = el => { App.logDate = el.dataset.date; Pages.logbook($('#main')); window.scrollTo(0, 0); };
Acts.shiftDate = el => {
  const next = addDays(App.logDate || App.me.today, +el.dataset.n);
  if (next > App.me.today) return;
  App.logDate = next; Pages.logbook($('#main'));
};
function collectLog() {
  const items = {};
  LB.order.forEach(code => {
    const s = LB.items[code];
    items[code] = { opening: s.opening, stocks: s.stocks, starting: s.starting, usage: s.usage, closing: s.closing };
  });
  const sales = parseExpr($('#sales').value);
  return { sales: sales == null || sales === undefined ? '' : sales, notes: $('#notes').value, items };
}
Acts.saveLog = async el => {
  const date = App.logDate || App.me.today;
  if (document.querySelector('input.bad')) return toast('Fix the red numbers first. Use digits, with + or − between them.', true);
  const body = collectLog();
  const neg = LB.order.filter(c => LB.items[c].usage != null && LB.items[c].usage < 0);
  if (neg.length) return toast(`${neg.join(', ')}: closing is higher than opening, so usage would be below zero. Check the count.`, true);
  if (!(parseFloat(body.sales) > 0) && !confirm('Sales is empty. Save the stock count now and add the sales later?')) return;
  const below = LB.order.filter(c => LB.items[c].ending != null && LB.items[c].ending < 0);
  if (below.length && !confirm(`${below.join(', ')} ${below.length > 1 ? 'end' : 'ends'} below zero (used more than the starting count). Save anyway?`)) return;
  el.disabled = true;
  try {
    await api(`/api/logs/${date}`, { method: 'PUT', body });
    localStorage.removeItem(`mapo_tmpl_${App.branch}_${date}`);
    toast('Saved.');
    Pages.logbook($('#main'));
  } catch (e) {
    if (e.offline) {
      const path = `/api/logs/${date}` + (isOwner() ? `?branch=${App.branch}` : '');
      queueWrite([...queueRead().filter(q => q.path !== path), { path, body }]);
      toast('No connection. Saved on this device and will upload when you\'re back online.');
    } else toast(e.message, true);
  } finally { el.disabled = false; }
};
Acts.deleteLog = async () => {
  const date = App.logDate || App.me.today;
  if (!confirm(`Delete the log for ${fmtDay(date)}? This can't be undone.`)) return;
  await api(`/api/logs/${date}`, { method: 'DELETE' });
  toast('Deleted.'); Pages.logbook($('#main'));
};

/* ---------- Forecast ---------- */
Pages.forecast = async main => {
  const days = App.fcDays || 14;
  const f = await api(`/api/forecast?days=${days}`);
  const rows = f.forecast.map(r => {
    const why = r.holiday ? `<span class="chip line">${esc(r.holiday)}</span>` : r.event ? '<span class="chip line">Your event</span>' : r.payday ? '<span class="chip line">Payday</span>' : '';
    return `<tr><td class="nowrap">${fmtDay(r.date)}</td><td class="num"><b>${peso(r.yhat)}</b></td><td class="num small muted nowrap">${peso(r.lower)} – ${peso(r.upper)}</td><td>${chip(r.tier, r.tier_label)}</td><td>${why}</td></tr>`;
  }).join('');
  const total = f.forecast.reduce((a, r) => a + r.yhat, 0);
  main.innerHTML = head('Forecast', f.model_label ? `Using: ${esc(f.model_label)} · ${f.n_obs} logged days` : '',
    `<div class="seg" role="group" aria-label="Horizon">${[7, 14, 30].map(n => `<button aria-pressed="${n === days}" data-act="fcDays" data-n="${n}">${n} days</button>`).join('')}</div>
     ${isManager() ? '<button class="btn ghost sm" data-act="fcRefresh">Refresh</button>' : ''}`) +
    (f.notes.length ? `<div class="banner" style="margin-bottom:14px">${f.notes.map(esc).join(' ')}</div>` : '') +
    (f.refreshing ? '<div class="banner" style="margin-bottom:14px">Comparing deeper models (Prophet, SARIMAX) in the background. This page updates itself.</div>' : '') +
    `<div class="card"><div class="row between"><h2>Sales: last 60 days and next ${days}</h2><b>${peso(total)} expected</b></div>${lineChart({ history: f.history, forecast: f.forecast })}</div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h2>Day by day</h2><div class="tbl-wrap"><table><thead><tr><th>Date</th><th class="num">Forecast</th><th class="num">Likely range</th><th>Tier</th><th>Driver</th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="empty">Not enough data yet.</td></tr>'}</tbody></table></div></div>
      <div>${f.accuracy ? accuracyCard(f.accuracy, f.model) : ''}${isManager() ? eventsCard(f) : upcomingCard(f)}</div>
    </div>`;
  main.onsubmit = async e => {
    e.preventDefault();
    await api('/api/events', { method: 'POST', body: { date: $('#evDate').value, name: $('#evName').value } });
    toast('Event added. The forecast will use it.'); Pages.forecast(main);
  };
  pollWhile(f.refreshing);
};
Acts.fcDays = el => { App.fcDays = +el.dataset.n; Pages.forecast($('#main')); };
Acts.fcRefresh = async () => { await api('/api/forecast/refresh', { method: 'POST' }); toast('Recomputing the forecast…'); Pages.forecast($('#main')); };
Acts.delEvent = async el => { await api(`/api/events/${el.dataset.id}`, { method: 'DELETE' }); Pages.forecast($('#main')); };

function accuracyCard(a, chosen) {
  const rows = a.models.map(m => `<tr><td>${m.name === chosen ? '<b>★ </b>' : ''}${esc(m.label)}</td><td class="num">${peso(m.mae)}</td><td class="num">${num(m.mape)}%</td><td class="num ${m.skill > 0 ? 'up' : ''}">${m.name === 'ma7' ? 'baseline' : (m.skill > 0 ? '+' : '') + num(m.skill, 0) + '%'}</td></tr>`).join('');
  return `<div class="card"><h2>How accurate is it?</h2>
    <p class="small muted" style="margin-top:0">Each model predicted ${a.horizon_days}-day windows it hadn't seen (${a.folds} test round${a.folds > 1 ? 's' : ''}). The lowest error wins. Average miss is shown per day.</p>
    <div class="tbl-wrap"><table><thead><tr><th>Model</th><th class="num">Avg miss</th><th class="num">% off</th><th class="num">vs 7-day avg</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${Object.keys(a.failed || {}).length ? `<div class="hint">Skipped: ${Object.keys(a.failed).map(esc).join(', ')} (couldn't run on this data).</div>` : ''}</div>`;
}
function upcomingCard(f) {
  return `<div class="card"><h2>Coming up</h2>${f.holidays.slice(0, 6).map(h => `<div class="row between small"><span>${esc(h.name)}</span><span class="muted">${fmtDay(h.date)}</span></div>`).join('') || '<div class="muted small">No holidays in the next 60 days.</div>'}</div>`;
}
function eventsCard(f) {
  return `<div class="card"><h2>Events that change sales</h2>
    <p class="small muted" style="margin-top:0">Paydays and national holidays are built in. Add school fairs, promos, road closures or anything else that moves sales.</p>
    ${f.events.map(e => `<div class="row between small" style="margin-bottom:6px"><span><b>${esc(e.name)}</b> · ${fmtDay(e.event_date)}</span><button class="btn ghost sm" data-act="delEvent" data-id="${e.id}" aria-label="Remove ${esc(e.name)}">Remove</button></div>`).join('')}
    <form class="row" style="margin-top:8px;align-items:flex-end"><div style="flex:1;min-width:130px"><label class="f" for="evDate">Date</label><input id="evDate" type="date" required></div>
    <div style="flex:2;min-width:150px"><label class="f" for="evName">Name</label><input id="evName" type="text" maxlength="60" required placeholder="School fair"></div><button class="btn sm">Add event</button></form>
    <h3 style="margin-top:14px">Coming up</h3>${f.holidays.slice(0, 4).map(h => `<div class="row between small"><span>${esc(h.name)}</span><span class="muted">${fmtDay(h.date)}</span></div>`).join('')}</div>`;
}

/* ---------- Stock ---------- */
Pages.stock = async main => {
  const r = await api('/api/reorder');
  const STATUS = { critical: ['deficit', 'Runs out before restock'], reorder: ['bad', 'Reorder now'], ok: ['acceptable', 'OK'], unknown: ['', 'Need more data'] };
  const rows = r.rows.map(x => `<tr><td><b>${esc(x.code)}</b> <span class="small muted">${esc(x.name.replace(/ \(.*\)/, ''))}</span></td><td class="num">${num(x.stock, 0)}</td><td class="num">${x.avg_daily != null ? num(x.avg_daily) : '—'}</td><td class="num">${x.days_cover != null ? num(x.days_cover) : '—'}</td><td>${STATUS[x.status][0] ? chip(STATUS[x.status][0], STATUS[x.status][1]) : `<span class="muted small">${STATUS[x.status][1]}</span>`}</td><td class="num"><b>${x.order_qty || ''}</b></td></tr>`).join('');
  main.innerHTML = head('Stock', r.stock_date ? `Last count ${fmtDay(r.stock_date, true)}` : 'No stock counts yet',
    r.message ? '<button class="btn gold" data-act="copyOrder">Copy supplier message</button>' : '') +
    `<div class="card"><h2>Cups on hand</h2>${cups(r.rows)}</div>
    <div class="card"><h2>What to order</h2><div class="tbl-wrap"><table><thead><tr><th>Item</th><th class="num">In stock</th><th class="num">Used / day</th><th class="num">Days left</th><th>Status</th><th class="num">Order</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="empty">Log a stock count to see order suggestions.</td></tr>'}</tbody></table></div>
    <div class="hint">Order = forecast use over the lead time plus your cover days, plus a safety cushion for busy swings, minus what you have. Set lead times and cover days in Settings.</div></div>`;
  Acts.copyOrder = async () => { await navigator.clipboard.writeText(r.message); toast('Order message copied. Paste it into Messenger or SMS.'); };
};

/* ---------- MAPO ---------- */
App.chat = App.chat || [];
Pages.mapo = async main => {
  if (!App.chat.length) App.chat.push({ who: 'bot', text: 'Hi, I\'m MAPO. Ask about sales, forecasts or stock, or tell me a closing count like "closing medio 5, grande 3". I\'ll ask you to confirm before changing anything.' });
  main.innerHTML = head('MAPO assistant', 'Ask in English or Taglish') + `<div class="card chat">
    <div class="msgs" id="msgs" aria-live="polite"></div>
    <div class="chips">${['Sales today', 'Forecast tomorrow', 'Forecast this week', 'Stock', 'What should I order?', 'Profit this week', 'Best sellers', 'Benta kahapon'].map(c => `<button data-act="chip" data-q="${esc(c)}">${esc(c)}</button>`).join('')}</div>
    <form class="composer" id="chatForm"><input id="chatIn" type="text" autocomplete="off" placeholder="Type a question or a stock update" aria-label="Message MAPO" maxlength="300"><button class="btn gold">Send</button></form></div>`;
  drawChat();
  main.onsubmit = async e => { e.preventDefault(); const v = $('#chatIn').value.trim(); if (v) { $('#chatIn').value = ''; send(v); } };
  $('#chatIn').focus();
};
function drawChat() {
  const box = $('#msgs'); if (!box) return;
  box.innerHTML = App.chat.map((m, i) => `<div class="msg ${m.who}">${md(m.text)}${m.pending ? `<div class="acts"><button class="btn gold sm" data-act="chatYes" data-i="${i}">Confirm</button><button class="btn ghost sm" data-act="chatNo" data-i="${i}">Cancel</button></div>` : ''}</div>`).join('');
  box.scrollTop = box.scrollHeight;
}
async function send(text) {
  // reserve the reply slot first so answers always appear in the order the questions were asked
  App.chat.push({ who: 'me', text });
  const slot = { who: 'bot', text: '…' };
  App.chat.push(slot); drawChat();
  try {
    const r = await api('/api/chat', { method: 'POST', body: { message: text } });
    slot.text = r.reply; slot.pending = r.pending ? r.pending.id : null;
  } catch (e) { slot.text = e.message; }
  drawChat();
}
async function answer(el, accept) {
  const m = App.chat[+el.dataset.i], id = m.pending; m.pending = null;
  try { const r = await api('/api/chat/confirm', { method: 'POST', body: { id, accept } }); App.chat.push({ who: 'bot', text: r.reply }); }
  catch (e) { App.chat.push({ who: 'bot', text: e.message }); }
  drawChat();
}
Acts.chip = el => send(el.dataset.q);
Acts.chatYes = el => answer(el, true);
Acts.chatNo = el => answer(el, false);

/* ---------- More (mobile) ---------- */
Pages.more = async main => {
  const extra = navFor().filter(n => !n.tab);
  main.innerHTML = head('More') + `<div class="card stack">${extra.map(n => `<button class="btn ghost" style="width:100%;text-align:left" data-act="go" data-id="${n.id}">${n.label}</button>`).join('')}
    <button class="btn ghost" style="width:100%;text-align:left" data-act="logout">Sign out (${esc(App.me.user.username)})</button></div>`;
};
