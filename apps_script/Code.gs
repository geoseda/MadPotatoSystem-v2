/**
 * Google Sheets backup for Mad Potato.
 * 1. Create a Google Sheet. Extensions > Apps Script. Paste this file.
 * 2. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 * 3. Copy the web app URL into Settings > Google Sheets backup.
 *
 * Every time a day is saved in the app, it is written here (one row per day in
 * "Daily", one row per item per day in "Stock"). Re-saving a day updates its rows.
 */
function doPost(e) {
  var data = JSON.parse(e.postData.contents);
  if (data.test) return json_({ ok: true, test: true });

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var daily = sheet_(ss, 'Daily', ['Branch', 'Date', 'Sales', 'Notes', 'Updated by', 'Saved at']);
  var stock = sheet_(ss, 'Stock', ['Branch', 'Date', 'Item', 'Starting', 'Restock', 'Ending', 'Used']);

  upsert_(daily, [data.branch, data.date], [data.branch, data.date, data.sales, data.notes, data.updated_by, new Date()]);
  deleteRows_(stock, data.branch, data.date);
  (data.items || []).forEach(function (it) {
    stock.appendRow([data.branch, data.date, it.code, it.starting, it.restock, it.ending, it.usage]);
  });
  return json_({ ok: true });
}

function sheet_(ss, name, headers) {
  var sh = ss.getSheetByName(name) || ss.insertSheet(name);
  if (sh.getLastRow() === 0) { sh.appendRow(headers); sh.setFrozenRows(1); }
  return sh;
}
function key_(v) { return v instanceof Date ? Utilities.formatDate(v, 'Asia/Manila', 'yyyy-MM-dd') : String(v); }
function upsert_(sh, keys, row) {
  var vals = sh.getDataRange().getValues();
  for (var i = 1; i < vals.length; i++) {
    if (key_(vals[i][0]) === keys[0] && key_(vals[i][1]) === keys[1]) {
      sh.getRange(i + 1, 1, 1, row.length).setValues([row]);
      return;
    }
  }
  sh.appendRow(row);
}
function deleteRows_(sh, branch, date) {
  var vals = sh.getDataRange().getValues();
  for (var i = vals.length - 1; i >= 1; i--) {
    if (key_(vals[i][0]) === branch && key_(vals[i][1]) === date) sh.deleteRow(i + 1);
  }
}
function json_(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
