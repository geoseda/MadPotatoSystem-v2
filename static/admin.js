'use strict';
/* Manager and owner pages: Insights, Reports, Settings. */

const qsBranch = () => (isOwner() && App.branch ? `?branch=${App.branch}` : '');

/* ---------- Insights ---------- */
Pages.insights = async main => {
  const days = App.profitDays || 30;
  const end = App.me.today, start = addDays(end, -(days - 1));
  const [ins, pr] = await Promise.all([api('/api/insights'), api(`/api/profit?start=${start}&end=${end}`)]);
  const t = pr.totals;
  const alerts = ins.anomalies.length ? ins.anomalies.map(x => `<div class="alert"><span class="dot ${x.severity}"></span><div><b>${esc(x.title)}</b><span class="small muted">${fmtDay(x.date)}</span><div class="small">${esc(x.detail)}</div></div></div>`).join('')
    : '<div class="empty">Nothing unusual. Sales and stock look consistent.</div>';
  const sellers = pr.best_sellers.map(r => `<tr><td><b>${esc(r.code)}</b> <span class="small muted">${esc(r.name.replace(/ \(.*\)/, ''))}</span></td><td class="num">${num(r.units, 0)} ${esc(r.unit)}</td><td class="num">${r.est_revenue ? peso(r.est_revenue) : '—'}</td><td class="num">${r.est_margin ? peso(r.est_margin) : '—'}</td></tr>`).join('');
  main.innerHTML = head('Insights', 'What is selling, what it earns, and what looks off',
    `<div class="seg" role="group" aria-label="Period">${[7, 30, 90].map(n => `<button aria-pressed="${n === days}" data-act="profitDays" data-n="${n}">${n} days</button>`).join('')}</div>`) +
    (!pr.costs_configured ? '<div class="banner" style="margin-bottom:14px">Ingredient costs aren\'t set, so margin and profit can\'t be calculated. <a href="#/settings">Add costs in Settings</a>.</div>' : '') +
    `<div class="grid g3">
      <div class="card kpi"><div class="label">Sales (${t.days} logged days)</div><div class="value">${peso(t.sales)}</div></div>
      <div class="card kpi"><div class="label">Gross margin</div><div class="value">${pr.costs_configured ? t.margin_pct + '%' : '—'}</div><div class="note">${pr.costs_configured ? 'Ingredient cost ' + peso(t.cogs) : ''}</div></div>
      <div class="card kpi"><div class="label">After operating cost</div><div class="value ${t.net >= 0 ? 'up' : 'down'}">${peso(t.net)}</div><div class="note">${peso(t.avg_net_per_day)} per day</div></div>
      <div class="card kpi"><div class="label">Break-even sales per day</div><div class="value">${peso(pr.breakeven_sales_per_day)}</div><div class="note">${pr.days_below_breakeven} of ${t.days} days below it</div></div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h2>Things to check</h2>${alerts}</div>
      <div class="card"><h2>Average sales by weekday</h2>${weekdayBars(ins.weekday.rows, ins.weekday.best_day)}<div class="hint">${ins.weekday.best_day ? `${esc(ins.weekday.best_day)} is your strongest day.` : ''}</div></div>
    </div>
    <div class="card" style="margin-top:14px"><h2>Best sellers (by units used)</h2><div class="tbl-wrap"><table><thead><tr><th>Item</th><th class="num">Units</th><th class="num">Est. revenue</th><th class="num">Est. margin</th></tr></thead><tbody>${sellers || '<tr><td colspan="4" class="empty">No usage logged in this period.</td></tr>'}</tbody></table></div>
    <div class="hint">Revenue per item is estimated as units × item price, because the logbook records one sales total per day.</div></div>`;
};
Acts.profitDays = el => { App.profitDays = +el.dataset.n; Pages.insights($('#main')); };

/* ---------- Reports ---------- */
Pages.reports = async main => {
  const period = App.period || 'week';
  const rows = await api(`/api/reports?period=${period}`);
  const body = rows.map(r => {
    const ch = r.avg_change_pct == null ? '' : `<span class="${r.avg_change_pct >= 0 ? 'up' : 'down'}">${r.avg_change_pct >= 0 ? '▲' : '▼'} ${Math.abs(r.avg_change_pct)}%</span>`;
    const tiers = Object.entries(r.tiers).map(([k, v]) => `<span class="chip ${k}" title="${TIER_LABEL[k]}">${v}</span>`).join(' ');
    return `<tr><td><b>${esc(r.period)}</b><div class="small muted">${fmtDay(r.from)} – ${fmtDay(r.to)}</div></td><td class="num">${r.days}</td><td class="num"><b>${peso(r.sales)}</b></td><td class="num">${peso(r.avg)}<div class="small">${ch}</div></td><td>${fmtDay(r.best_day)}<div class="small muted">${peso(r.best_sales)}</div></td><td>${tiers}</td></tr>`;
  }).join('');
  main.innerHTML = head('Reports', 'Results by period',
    `<div class="seg" role="group" aria-label="Period">${[['week', 'Weekly'], ['month', 'Monthly']].map(([k, l]) => `<button aria-pressed="${k === period}" data-act="period" data-p="${k}">${l}</button>`).join('')}</div>
     <a class="btn gold" href="/api/export/pdf${qsBranch()}">Download PDF</a><a class="btn ghost" href="/api/export/excel${qsBranch()}">Download Excel</a>`) +
    `<div class="card"><div class="tbl-wrap"><table><thead><tr><th>${period === 'week' ? 'Week' : 'Month'}</th><th class="num">Days</th><th class="num">Sales</th><th class="num">Avg / day</th><th>Best day</th><th>Days by tier</th></tr></thead><tbody>${body || '<tr><td colspan="6" class="empty">No sales logged yet.</td></tr>'}</tbody></table></div>
    <div class="hint">The PDF is a one-page summary for the owner. The Excel file has every day, stock log, profit, forecast and reorder list.</div></div>`;
};
Acts.period = el => { App.period = el.dataset.p; Pages.reports($('#main')); };

/* ---------- Settings ---------- */
const field = (id, label, val, extra = '', hint = '') => `<div class="field"><label class="f" for="${id}">${label}</label><input id="${id}" ${extra} value="${esc(val)}">${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;

Pages.settings = async main => {
  const [st, items, users, audit, ] = await Promise.all([api('/api/settings'), api('/api/items'), api('/api/users'), api('/api/audit?limit=40')]);
  const s = st.settings;
  const branches = App.me.branches;
  const itemRows = items.map(i => `<tr data-item="${esc(i.code)}"><td><b>${esc(i.code)}</b> <span class="small muted">${esc(i.name.replace(/ \(.*\)/, ''))}</span></td>
    ${[['unit_cost', i.unit_cost, '0.01'], ['unit_price', i.unit_price, '0.01'], ['reorder_point', i.reorder_point, '1'], ['lead_time_days', i.lead_time_days, '1']].map(([k, v, step]) => `<td class="num"><input type="number" min="0" step="${step}" data-f="${k}" value="${v}" aria-label="${esc(i.code)} ${k}"></td>`).join('')}
    <td><input type="checkbox" data-f="active" ${i.active ? 'checked' : ''} aria-label="${esc(i.code)} active"></td></tr>`).join('');
  const userRows = users.map(u => `<tr><td><b>${esc(u.username)}</b><div class="small muted">${esc(u.display_name || '')}</div></td><td>${esc(u.role)}</td><td>${esc(u.branch_name || 'All')}</td><td class="small muted">${esc(u.last_login || 'never')}</td>
    <td class="right nowrap">${u.id === App.me.user.id ? '' : `<button class="btn ghost sm" data-act="toggleUser" data-id="${u.id}" data-on="${u.active ? 0 : 1}">${u.active ? 'Deactivate' : 'Activate'}</button> <button class="btn ghost sm" data-act="resetPw" data-id="${u.id}">Reset password</button>`}</td></tr>`).join('');
  const auditRows = audit.map(a => `<tr><td class="small nowrap">${esc(a.ts.slice(5, 16))}</td><td>${esc(a.username || '')}</td><td>${esc(a.action.replace(/_/g, ' '))}</td><td class="small muted">${esc(auditDetail(a.detail))}</td></tr>`).join('');
  main.innerHTML = head('Settings', esc((branches.find(b => b.id == App.branch) || {}).name || '')) + `
  <div class="grid g2">
    <form class="card" id="rulesForm"><h2>Business rules</h2>
      ${field('op_cost', 'Operating cost per day (₱)', s.op_cost, 'type="number" min="0" step="1"', 'The minimum a day must earn to cover costs.')}
      <div class="grid" style="grid-template-columns:1fr 1fr;gap:0 12px">
        ${field('tier_deficit', 'Deficit below (₱)', s.tier_deficit, 'type="number" min="0"')}
        ${field('tier_under', 'Underperforming below (₱)', s.tier_under, 'type="number" min="0"')}
        ${field('tier_below', 'Below average below (₱)', s.tier_below, 'type="number" min="0"')}
        ${field('tier_high', 'High-performing above (₱)', s.tier_high, 'type="number" min="0"')}
      </div>
      ${field('cover_days', 'Days of stock a reorder should cover', s.cover_days, 'type="number" min="1" max="30"')}
      <div class="field"><label><input type="checkbox" id="alerts_enabled" ${s.alerts_enabled === '1' ? 'checked' : ''}> Send low-stock alerts ${st.telegram_configured ? '(Telegram is connected)' : '(Telegram is not set up on the server)'}</label></div>
      <button class="btn gold">Save rules</button></form>
    <form class="card" id="sheetsForm"><h2>Google Sheets backup</h2>
      ${field('sheets_url', 'Apps Script web app link', s.sheets_url, 'type="url" placeholder="https://script.google.com/macros/s/…/exec"', 'Every saved day is also sent here. See apps_script/Code.gs.')}
      <div class="small muted" style="margin-bottom:10px">Last sync: ${esc(s.sheets_last_status || 'none yet')}</div>
      <div class="row"><button class="btn gold">Save link</button><button type="button" class="btn ghost" data-act="testSheets">Send a test</button></div>
      <h3 style="margin-top:22px">Import history</h3>
      <p class="small muted" style="margin:4px 0 8px">Upload the old sales_data.csv: one row per item per day (Date, Item_Code, Starting_Stock, Ending_Stock, Daily_Usage, Daily_Total_Sales), or one row per day. Importing the same file again is safe.</p>
      <div class="row"><input type="file" id="csvFile" accept=".csv,text/csv" style="flex:1"><button type="button" class="btn ghost" data-act="importCsv">Import</button></div>
      <div id="importOut" class="small" style="margin-top:8px"></div></form>
  </div>
  <div class="card" style="margin-top:14px"><h2>Items, costs and reorder rules</h2>
    <div class="tbl-wrap"><table class="logtbl"><thead><tr><th>Item</th><th class="num">Cost each (₱)</th><th class="num">Price each (₱)</th><th class="num">Minimum stock</th><th class="num">Lead time (days)</th><th>Active</th></tr></thead><tbody>${itemRows}</tbody></table></div>
    <div class="hint">Cost drives margin and profit. Lead time is how many days a supplier takes. Minimum stock is an optional floor; the system also works out a safe level from your usage.</div>
    <button class="btn gold" style="margin-top:10px" data-act="saveItems">Save items</button></div>
  <div class="card" style="margin-top:14px"><h2>Team</h2>
    <div class="tbl-wrap"><table><thead><tr><th>User</th><th>Role</th><th>Branch</th><th>Last sign-in</th><th></th></tr></thead><tbody>${userRows}</tbody></table></div>
    <form id="userForm" class="row" style="margin-top:12px;align-items:flex-end">
      <div><label class="f" for="nu">Username</label><input id="nu" type="text" required maxlength="30" style="width:140px"></div>
      <div><label class="f" for="nd">Name</label><input id="nd" type="text" maxlength="40" style="width:150px"></div>
      <div><label class="f" for="np">Password</label><input id="np" type="password" required minlength="8" autocomplete="new-password" style="width:150px"></div>
      ${isOwner() ? `<div><label class="f" for="nr">Role</label><select id="nr"><option value="staff">Staff</option><option value="manager">Manager</option><option value="owner">Owner</option></select></div>
      <div><label class="f" for="nb">Branch</label><select id="nb">${branches.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('')}</select></div>` : ''}
      <button class="btn sm">Add user</button></form>
    <div class="hint">Staff can log today and yesterday, use MAPO, and see stock and forecasts. Managers also see profit, reports and settings. Owners manage branches and users.</div></div>
  ${isOwner() ? `<div class="grid g2" style="margin-top:14px">
    <form class="card" id="branchForm"><h2>Branches</h2><div class="small" style="margin-bottom:8px">${branches.map(b => esc(b.name)).join(' · ')}</div>
      <div class="row" style="align-items:flex-end"><div><label class="f" for="bn">Name</label><input id="bn" type="text" required maxlength="60" placeholder="Mad Potato - Antipolo"></div><div><label class="f" for="bc">Short code</label><input id="bc" type="text" required maxlength="12" placeholder="ANTIPOLO" style="width:120px"></div><button class="btn sm">Add branch</button></div></form>
    <div class="card"><h2>Backup</h2><p class="small muted" style="margin-top:0">Download every branch's logs as a file you can keep. Restoring merges the file into the system.</p>
      <div class="row"><a class="btn gold" href="/api/backup">Download backup</a><input type="file" id="restoreFile" accept=".json,application/json" style="flex:1;min-width:140px"><button class="btn ghost" data-act="restore">Restore</button></div></div></div>` : ''}
  <div class="grid g2" style="margin-top:14px">
    <form class="card" id="pwForm"><h2>My password</h2>
      ${field('pw_cur', 'Current password', '', 'type="password" autocomplete="current-password" required')}
      ${field('pw_new', 'New password (8+ characters)', '', 'type="password" autocomplete="new-password" required minlength="8"')}
      <button class="btn">Change password</button></form>
    <div class="card"><h2>Recent activity</h2><div class="tbl-wrap" style="max-height:300px;overflow-y:auto"><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Detail</th></tr></thead><tbody>${auditRows}</tbody></table></div></div>
  </div>`;

  const Forms = {
    rulesForm: async () => {
      await api('/api/settings', { method: 'PUT', body: { op_cost: $('#op_cost').value, tier_deficit: $('#tier_deficit').value, tier_under: $('#tier_under').value, tier_below: $('#tier_below').value, tier_high: $('#tier_high').value, cover_days: $('#cover_days').value, alerts_enabled: $('#alerts_enabled').checked ? '1' : '0' } });
      toast('Rules saved. The forecast will refresh.');
    },
    sheetsForm: async () => { await api('/api/settings', { method: 'PUT', body: { sheets_url: $('#sheets_url').value } }); toast('Link saved.'); },
    userForm: async () => {
      await api('/api/users', { method: 'POST', body: { username: $('#nu').value, display_name: $('#nd').value, password: $('#np').value, role: ($('#nr') || {}).value || 'staff', branch_id: ($('#nb') || {}).value || App.me.user.branch_id } });
      toast('User added.'); Pages.settings(main);
    },
    branchForm: async () => {
      await api('/api/branches', { method: 'POST', body: { name: $('#bn').value, code: $('#bc').value } });
      toast('Branch added.'); App.me = await api('/api/auth/me'); renderShell(); markNav(); Pages.settings(main);
    },
    pwForm: async () => { await api('/api/auth/password', { method: 'POST', body: { current: $('#pw_cur').value, new: $('#pw_new').value } }); toast('Password changed.'); $('#pw_cur').value = $('#pw_new').value = ''; },
  };
  main.onsubmit = async e => { e.preventDefault(); const f = Forms[e.target.id]; if (f) { try { await f(); } catch (ex) { toast(ex.message, true); } } };
};

function auditDetail(d) {
  const bits = [];
  for (const [k, v] of Object.entries(d || {})) {
    if (Array.isArray(v)) bits.push(`${k}: ${v.join(', ')}`);
    else if (typeof v === 'object' && v) continue;
    else bits.push(`${k}: ${v}`);
  }
  return bits.slice(0, 4).join(' · ');
}

Acts.saveItems = async () => {
  const body = [...document.querySelectorAll('tr[data-item]')].map(tr => {
    const o = { code: tr.dataset.item };
    tr.querySelectorAll('[data-f]').forEach(i => { o[i.dataset.f] = i.type === 'checkbox' ? i.checked : i.value; });
    return o;
  });
  await api('/api/items', { method: 'PUT', body });
  toast('Items saved.');
};
Acts.testSheets = async () => { await api('/api/settings/test-sheets', { method: 'POST' }); toast('Test sent. Check your sheet.'); };
Acts.importCsv = async () => {
  const f = $('#csvFile').files[0];
  if (!f) return toast('Choose a CSV file first.', true);
  const fd = new FormData(); fd.append('file', f);
  const r = await api('/api/import/csv', { method: 'POST', form: fd });
  const range = r.first_date ? ` from <b>${esc(fmtDay(r.first_date))}</b> to <b>${esc(fmtDay(r.last_date))}</b>` : '';
  $('#importOut').innerHTML = `Imported <b>${r.imported}</b> day(s)${range}. Items found: ${esc((r.matched_items || []).join(', ') || 'none')}.`
    + (r.has_cup_columns ? '<br>Opening, Stocks and Closing were imported for the cup items.' : '')
    + (r.negative_stock ? `<br>${r.negative_stock} stock count(s) were below zero in your sheet. They are kept as written.` : '')
    + (r.usage_mismatch ? `<br>${r.usage_mismatch} row(s) where Starting − Usage didn't equal End. Kept as written (a purchase or adjustment that day).` : '')
    + (r.skipped_count ? `<br>Skipped ${r.skipped_count}: ${r.skipped.map(esc).join('; ')}` : '')
    + (r.unknown_items && r.unknown_items.length ? `<br>Unknown item codes ignored: ${r.unknown_items.map(esc).join(', ')}` : '');
  toast('Import finished.');
};
Acts.toggleUser = async el => { await api(`/api/users/${el.dataset.id}`, { method: 'PATCH', body: { active: el.dataset.on === '1' } }); Pages.settings($('#main')); };
Acts.resetPw = async el => {
  const pw = prompt('New password for this user (8+ characters):');
  if (!pw) return;
  await api(`/api/users/${el.dataset.id}`, { method: 'PATCH', body: { password: pw } });
  toast('Password reset.');
};
Acts.restore = async () => {
  const f = $('#restoreFile').files[0];
  if (!f) return toast('Choose a backup file first.', true);
  if (!confirm('Merge this backup into the system? Days in the file replace the same days here.')) return;
  const fd = new FormData(); fd.append('file', f);
  const r = await api('/api/restore', { method: 'POST', form: fd });
  toast(`Restored ${r.restored_days} day(s).`);
};
