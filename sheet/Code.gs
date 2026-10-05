const KINDS = ['daily', 'weekly', 'todo'];
const FIELDS = ['id', 'kind', 'title', 'due', 'done', 'done_on', 'sent', 'sent_for'];

function locked(fn) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try { return fn(); }
  finally {
    try { SpreadsheetApp.flush(); }
    finally { lock.releaseLock(); }
  }
}

function book() {
  return SpreadsheetApp.openById(PropertiesService.getScriptProperties().getProperty('SHEET_ID'));
}

function today() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
}

function monday(day) {
  const tz = Session.getScriptTimeZone();
  const n = Utilities.parseDate(day + ' 12:00', tz, 'yyyy-MM-dd HH:mm');
  // Noon keeps calendar dates stable across a daylight-saving change.
  n.setTime(n.getTime() - (Number(Utilities.formatDate(n, tz, 'u')) - 1) * 86400000);
  return Utilities.formatDate(n, tz, 'yyyy-MM-dd');
}

function read(sheet) {
  const n = sheet.getLastRow() - 1;
  if (n < 1) return [];
  const left = sheet.getRange(2, 1, n, 4).getDisplayValues();
  const done = sheet.getRange(2, 5, n, 1).getValues();
  const right = sheet.getRange(2, 6, n, 3).getDisplayValues();
  return left.map((cells, i) => {
    const values = [...cells, done[i][0] === true || done[i][0] === 'TRUE', ...right[i]];
    if (values.every((v, j) => j === 4 ? !v : v === '')) return null;
    const row = Object.fromEntries(FIELDS.map((f, j) => [f, values[j]]));
    try { row.sent = JSON.parse(row.sent || '[]'); } catch (e) { /* Python flags bad rows. */ }
    row.row = i + 2;
    return row;
  }).filter(Boolean);
}

function cell(sheet, row, field, value) {
  const range = sheet.getRange(row, FIELDS.indexOf(field) + 1);
  if (field === 'done') range.setValue(value === true || value === 'TRUE');
  else {
    let text = field === 'sent' && Array.isArray(value) ? JSON.stringify(value) : String(value == null ? '' : value);
    // setValue still treats a leading '=' as a formula; the apostrophe keeps it literal.
    if (text.startsWith('=')) text = "'" + text;
    range.setNumberFormat('@').setValue(text);
  }
}

function validation(sheet, start, n) {
  sheet.getRange(start, 2, n, 1).setDataValidation(SpreadsheetApp.newDataValidation()
    .requireValueInList(KINDS, true).setAllowInvalid(true).build());
  sheet.getRange(start, 5, n, 1).setDataValidation(SpreadsheetApp.newDataValidation()
    .requireCheckbox().setAllowInvalid(true).build());
  const hhmm = '([01]?[0-9]|2[0-3]):[0-5][0-9]';
  const weekday = '(mon(day)?|tue(s(day)?)?|wed(nesday)?|thu(rs(day)?)?|fri(day)?|sat(urday)?|sun(day)?)';
  const b = '$B' + start, d = '$D' + start;
  const formula = '=OR(AND(' + b + '="daily",REGEXMATCH(' + d + ',"^' + hhmm + '$")),' +
    'AND(' + b + '="weekly",REGEXMATCH(LOWER(' + d + '),"^' + weekday + '( ' + hhmm + ')?$")),' +
    'AND(' + b + '="todo",REGEXMATCH(' + d + ',"^[0-9]{4}-[0-9]{2}-[0-9]{2} ' + hhmm.replace('?', '') + '$")))';
  sheet.getRange(start, 4, n, 1).setDataValidation(SpreadsheetApp.newDataValidation()
    .requireFormulaSatisfied(formula).setAllowInvalid(true).build());
}

function reset(ss, rows) {
  const meta = ss.getSheetByName('meta').getRange('B1');
  const last = meta.getDisplayValues()[0][0], day = today();
  if (last >= day) return [];
  const sheet = ss.getSheetByName('tasks'), missed = [];
  const kinds = monday(last) !== monday(day) ? ['daily', 'weekly'] : ['daily'];
  for (const row of rows) {
    if (kinds.includes(row.kind)) {
      if (!row.done && Array.isArray(row.sent) && !row.sent.includes('missed'))
        missed.push({kind: row.kind, title: row.title, due: row.due});
      cell(sheet, row.row, 'done', false);
      cell(sheet, row.row, 'sent', []);
    }
  }
  for (const row of rows.slice().reverse())
    if (row.kind === 'todo' && row.done) sheet.deleteRow(row.row);
  meta.setNumberFormat('@').setValue(day);
  return missed;
}

function rollover() {
  return locked(() => {
    const ss = book();
    return reset(ss, read(ss.getSheetByName('tasks')));
  });
}

function doPost(e) {
  let result;
  try {
    result = locked(() => {
      const body = JSON.parse(e.postData.contents);
      const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
      if (!token || body.token !== token) throw new Error('bad token');
      if (!['load', 'add', 'update', 'delete'].includes(body.op)) throw new Error('bad op');
      const ss = book(), sheet = ss.getSheetByName('tasks');
      const missed = reset(ss, read(sheet));
      let rows = read(sheet);
      for (const row of rows) {
        if (!row.id) {
          row.id = 't' + Utilities.getUuid().replace(/-/g, '').slice(0, 8);
          cell(sheet, row.row, 'id', row.id);
        }
      }
      if (body.op === 'add') {
        if (!KINDS.includes(body.kind) || !/^t[0-9a-f]{8}$/.test(body.task.id)) throw new Error('bad task');
        if (!rows.some(row => row.id === body.task.id)) {
          const n = sheet.getLastRow() + 1;
          if (n > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), 1);
          validation(sheet, n, 1);
          const task = {...body.task, kind: body.kind};
          for (const field of FIELDS) cell(sheet, n, field, task[field]);
        }
      } else if (body.op === 'update' || body.op === 'delete') {
        const row = rows.find(row => row.id === body.id);
        if (!row) throw new Error('task gone');
        if (body.op === 'delete') sheet.deleteRow(row.row);
        else {
          for (const field of Object.keys(body.fields))
            if (!FIELDS.slice(2).includes(field)) throw new Error('bad field');
          for (const [field, value] of Object.entries(body.fields)) cell(sheet, row.row, field, value);
        }
      }
      const state = {last_reset: ss.getSheetByName('meta').getRange('B1').getDisplayValues()[0][0],
                     daily: [], weekly: [], todo: [], bad_rows: 0};
      for (const row of read(sheet)) {
        const {kind, row: n, ...task} = row;
        if (KINDS.includes(kind)) state[kind].push(task);
        else state.bad_rows++;
      }
      return {ok: true, state, missed};
    });
  } catch (e) { result = {ok: false, error: e.message}; }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}

function setup() {
  locked(() => {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    PropertiesService.getScriptProperties().setProperty('SHEET_ID', ss.getId());
    const tasks = ss.getSheetByName('tasks') || ss.insertSheet('tasks');
    const meta = ss.getSheetByName('meta') || ss.insertSheet('meta');
    const n = tasks.getMaxRows() - 1;
    tasks.getRange(1, 1, tasks.getMaxRows(), 4).setNumberFormat('@');
    tasks.getRange(1, 6, tasks.getMaxRows(), 3).setNumberFormat('@');
    tasks.getRange(1, 1, 1, 8).setValues([FIELDS]);
    tasks.setFrozenRows(1);
    validation(tasks, 2, n);
    meta.getRange('A1:B1').setNumberFormat('@');
    meta.getRange('A1').setValue('last_reset');
    if (!meta.getRange('B1').getDisplayValues()[0][0]) meta.getRange('B1').setValue(today());
    if (!ScriptApp.getProjectTriggers().some(trigger => trigger.getHandlerFunction() === 'rollover'))
      ScriptApp.newTrigger('rollover').timeBased().everyDays(1).atHour(0).create();
  });
}
