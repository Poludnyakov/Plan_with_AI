const { chromium } = require('playwright');
const fs = require('fs');
const MAX = process.env.PLATFORM === 'max';
const BASE = MAX ? 'http://127.0.0.1:8078' : 'http://127.0.0.1:8077';
const ENTRY = MAX ? '/max/miniapp' : '/miniapp';
const TIMELINE = MAX ? '/max/timeline' : '/mini-timeline';
const initData = fs.readFileSync(__dirname + '/initdata.txt', 'utf8').trim();
const results = [];
const check = (name, ok, detail = '') => { results.push([name, !!ok]); console.log(`${ok ? '✔' : '✘'} ${name}${!ok && detail ? ' — ' + detail : ''}`); };
const pad = n => String(n).padStart(2, '0');
const local = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;

async function deselect(page) {
  // Changing the day and back clears the selection, as a user would.
  await page.keyboard.press('ArrowRight'); await page.waitForTimeout(100);
  await page.keyboard.press('ArrowLeft'); await page.waitForTimeout(200);
}
async function openEditorFor(page, title) {
  await deselect(page);
  const node = page.locator('#timeline .calendar-event:not(.schedule-event)', { hasText: title });
  await node.click(); await page.waitForTimeout(150); await node.click(); await page.waitForTimeout(300);
}
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, locale: 'ru-RU', timezoneId: 'Europe/Moscow' });
  const page = await context.newPage();
  const errors = [], failed = [], dialogs = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) failed.push(`${r.status()} ${r.request().method()} ${r.url()}`); });
  page.on('dialog', async d => { dialogs.push(d.message()); await d.accept(); });
  await page.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({ contentType: 'application/javascript', body:
    `window.Telegram={WebApp:{initData:${JSON.stringify(initData)},platform:'android',version:'8.0',ready(){},expand(){},sendData(){},themeParams:{},colorScheme:'light'}};` }));
  await page.route('https://st.max.ru/js/max-web-app.js', route => route.fulfill({ contentType: 'application/javascript', body:
    `window.WebApp={initData:${JSON.stringify(initData)},platform:'android',ready(){},expand(){}};` }));

  await page.goto(BASE + ENTRY);
  await page.waitForURL(new RegExp(TIMELINE), { timeout: 10000 }).catch(() => {});
  check('вход через Telegram initData ведёт в календарь', page.url().includes(TIMELINE), page.url());
  await page.waitForTimeout(1200);
  await page.screenshot({ path: __dirname + '/' + (MAX ? 'max_' : '') + '01_today.png' });

  // Tomorrow has the seeded events.
  const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate() + 1); tomorrow.setHours(0,0,0,0);
  const key = `${tomorrow.getFullYear()}-${pad(tomorrow.getMonth()+1)}-${pad(tomorrow.getDate())}`;
  await page.click(`.week-day[data-day="${key}"]`);
  await page.waitForTimeout(500);
  const titles = await page.$$eval('#timeline .event-title, #timeline .all-day-event', ns => ns.map(n => n.textContent));
  check('завтрашние события видны на шкале', titles.some(t => t.includes('Утренняя встреча')), JSON.stringify(titles));
  check('занятие из расписания показано фоном', titles.some(t => t.includes('Занятие · Матанализ')), JSON.stringify(titles));
  check('XSS в названии не исполняется', !dialogs.some(d => d.includes('1')) && titles.some(t => t.includes('<img')), JSON.stringify(dialogs));
  check('ночное событие 23:00–01:00 видно', titles.some(t => t.includes('Ночная смена')), JSON.stringify(titles));
  await page.screenshot({ path: __dirname + '/' + (MAX ? 'max_' : '') + '02_tomorrow.png', fullPage: false });

  // The day after the night shift: the shift still runs until 01:00.
  const after = new Date(tomorrow); after.setDate(after.getDate() + 1);
  const afterKey = `${after.getFullYear()}-${pad(after.getMonth()+1)}-${pad(after.getDate())}`;
  await page.click(`.week-day[data-day="${afterKey}"]`); await page.waitForTimeout(300);
  const afterTitles = await page.$$eval('#timeline .event-title', ns => ns.map(n => n.textContent));
  check('ночная смена видна и в день окончания (00:00–01:00)', afterTitles.some(t => t.includes('Ночная смена')), JSON.stringify(afterTitles));

  // «＋ Добавить» from the header.
  await page.click(`.week-day[data-day="${key}"]`); await page.waitForTimeout(300);
  await page.click('#addEventButton'); await page.waitForTimeout(300);
  const startValue = await page.inputValue('#start-at'), endValue = await page.inputValue('#end-at');
  check('кнопка «＋ Добавить» заполняет начало и окончание', startValue && endValue, `start=${JSON.stringify(startValue)} end=${JSON.stringify(endValue)}`);
  await page.fill('#title', 'Созвон из мини-приложения');
  const s = new Date(tomorrow); s.setHours(16, 0); const e = new Date(tomorrow); e.setHours(17, 0);
  await page.fill('#start-at', local(s)); await page.fill('#end-at', local(e));
  await page.click('#createSave'); await page.waitForTimeout(1200);
  const createErr = await page.textContent('#createError');
  const afterCreate = await page.$$eval('#timeline .event-title', ns => ns.map(n => n.textContent));
  check('создание события через форму', afterCreate.includes('Созвон из мини-приложения'), createErr);
  await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open')));

  // Overlap with a confirmed event.
  await page.click('#addEventButton'); await page.waitForTimeout(200);
  await page.fill('#title', 'Пересекается со встречей');
  const s2 = new Date(tomorrow); s2.setHours(10, 30); const e2 = new Date(tomorrow); e2.setHours(11, 30);
  await page.fill('#start-at', local(s2)); await page.fill('#end-at', local(e2));
  await page.click('#createSave'); await page.waitForTimeout(1000);
  const overlapMsg = (await page.textContent('#createError')) || '';
  const toastText = (await page.textContent('#toast')) || '';
  check('пересечение с событием отслеживается', /пересек|занят|конфликт|перекрыва/i.test(overlapMsg + toastText), `error=${overlapMsg} toast=${toastText}`);
  await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open')));

  // End before start.
  await page.click('#addEventButton'); await page.waitForTimeout(200);
  await page.fill('#title', 'Наоборот');
  await page.fill('#start-at', local(e)); await page.fill('#end-at', local(s));
  await page.click('#createSave'); await page.waitForTimeout(800);
  const backwards = (await page.textContent('#createError')) || '';
  check('окончание раньше начала — понятная ошибка', /окончани|позже/i.test(backwards), backwards);
  await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open')));

  // Timed event during the all-day business trip: a warning dialog.
  const tripDay = new Date(tomorrow); tripDay.setDate(tripDay.getDate() + 2);
  const tripKey = `${tripDay.getFullYear()}-${pad(tripDay.getMonth()+1)}-${pad(tripDay.getDate())}`;
  await page.click(`.week-day[data-day="${tripKey}"]`).catch(()=>{}); await page.waitForTimeout(300);
  const before = dialogs.length;
  await page.click('#addEventButton'); await page.waitForTimeout(200);
  await page.fill('#title', 'Ужин в командировке');
  const s3 = new Date(tripDay); s3.setHours(19); const e3 = new Date(tripDay); e3.setHours(20);
  await page.fill('#start-at', local(s3)); await page.fill('#end-at', local(e3));
  await page.click('#createSave'); await page.waitForTimeout(1200);
  check('предупреждение о событии на весь день', dialogs.slice(before).some(d => d.includes('Командировка')), JSON.stringify(dialogs.slice(before)));
  await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open')));

  // Edit: select, open, rename.
  await page.click(`.week-day[data-day="${key}"]`); await page.waitForTimeout(300);
  await openEditorFor(page, 'Созвон из мини-приложения');
  check('двойное нажатие открывает редактор', await page.isVisible('#editModal.open'));
  await page.fill('#editTitle', 'Созвон (переименован)');
  await page.click('#editSave'); await page.waitForTimeout(1000);
  const renamed = await page.$$eval('#timeline .event-title', ns => ns.map(n => n.textContent));
  check('изменение названия сохраняется', renamed.includes('Созвон (переименован)'), (await page.textContent('#editError')) || JSON.stringify(renamed));

  // Drag by one hour.
  await deselect(page);
  const ev = page.locator('#timeline .calendar-event:not(.schedule-event)', { hasText: 'Созвон (переименован)' });
  await ev.click(); await page.waitForTimeout(200);
  const box = await ev.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + 10); await page.mouse.down();
  for (let i = 1; i <= 6; i++) { await page.mouse.move(box.x + box.width / 2, box.y + 10 + i * 10); await page.waitForTimeout(30); }
  await page.mouse.up(); await page.waitForTimeout(1500);
  const timeText = await page.locator('#timeline .calendar-event', { hasText: 'Созвон (переименован)' }).locator('.event-time').textContent();
  check('перетаскивание сдвигает событие на час', timeText.startsWith('17:00'), timeText + ' toast=' + (await page.textContent('#toast')));

  // Complete and delete.
  await openEditorFor(page, 'Созвон (переименован)');
  await page.click('#completeEvent'); await page.waitForTimeout(800);
  check('отметка «Выполнено»', await page.locator('#timeline .calendar-event.done', { hasText: 'Созвон (переименован)' }).count() === 1);
  await openEditorFor(page, 'Созвон (переименован)');
  await page.click('#deleteEvent'); await page.waitForTimeout(1000);
  check('удаление события', await page.locator('#timeline .calendar-event', { hasText: 'Созвон (переименован)' }).count() === 0);

  // Custom reminder after the start.
  await page.click('#addEventButton'); await page.waitForTimeout(200);
  await page.fill('#title', 'С напоминанием');
  await page.fill('#start-at', local(s)); await page.fill('#end-at', local(e));
  await page.click('[data-add-reminder="createReminders"]');
  await page.selectOption('#createReminders select', 'custom');
  const late = new Date(s); late.setHours(18);
  await page.fill('#createReminders input[type="datetime-local"]', local(late));
  await page.click('#createSave'); await page.waitForTimeout(600);
  check('напоминание позже начала отклоняется', /не позже начала/.test(await page.textContent('#createError') || ''));
  await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open')));

  // All-day via the form.
  await page.click('#addEventButton'); await page.waitForTimeout(200);
  await page.fill('#title', 'Выходной');
  await page.check('#all-day');
  await page.click('#createSave'); await page.waitForTimeout(1200);
  const lane = await page.$$eval('#timeline .all-day-event', ns => ns.map(n => n.textContent));
  check('событие на весь день из формы', lane.includes('Выходной'), (await page.textContent('#createError')) + JSON.stringify(lane));
  await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open')));

  // Agenda view and date picker, keyboard.
  await page.goto(BASE + TIMELINE + '?view=agenda'); await page.waitForTimeout(1200);
  const agenda = await page.$$eval('#agendaScroll .agenda-title', ns => ns.map(n => n.textContent));
  check('список (agenda) показывает события и занятия', agenda.some(t => t.includes('Утренняя встреча')) && agenda.some(t => t.includes('Занятие')), JSON.stringify(agenda.slice(0, 8)));
  await page.screenshot({ path: __dirname + '/' + (MAX ? 'max_' : '') + '03_agenda.png' });
  await page.goto(BASE + TIMELINE); await page.waitForTimeout(800);
  const label0 = await page.textContent('#dateLabel');
  await page.keyboard.press('ArrowRight'); await page.waitForTimeout(200);
  check('стрелка вправо — следующий день', (await page.textContent('#dateLabel')) !== label0);
  // Ribbon scrolls far without breaking.
  await page.evaluate(() => { const w = document.getElementById('weeks'); for (let i = 0; i < 30; i++) w.scrollLeft += 2000; });
  await page.waitForTimeout(300);
  check('лента дат бесконечно прокручивается', await page.$$eval('.week-day', ns => ns.length) > 180);

  // History tab: delete the whole import, then one event.
  await page.goto(BASE + TIMELINE); await page.waitForTimeout(800);
  await page.click('[data-calendar-view="history"]'); await page.waitForTimeout(1000);
  const history = await page.$$eval('#historyScroll .history-title', ns => ns.map(n => n.textContent));
  check('вкладка «История» показывает импорт и события', history.includes('Импорт расписания') && history.includes('Утренняя встреча'), JSON.stringify(history));
  await page.screenshot({ path: __dirname + '/' + (MAX ? 'max_' : '') + '04_history.png' });
  const asked = dialogs.length;
  await page.locator('#historyScroll .history-item', { hasText: 'Импорт расписания' }).locator('.history-delete').click();
  await page.waitForTimeout(1200);
  check('удаление импорта просит подтверждение', (dialogs[asked] || '').includes('Все занятия этого импорта'), dialogs[asked]);
  check('после удаления импорта — сообщение об итоге', /Импорт удалён/.test(await page.textContent('#toast')), await page.textContent('#toast'));
  const undone = await page.locator('#historyScroll .history-item.undone', { hasText: 'Импорт расписания' }).count();
  check('импорт помечен как отменённый, без кнопки', undone === 1);
  await page.click('[data-calendar-view="timeline"]');
  await page.click(`.week-day[data-day="${key}"]`); await page.waitForTimeout(400);
  const left = await page.$$eval('#timeline .event-title', ns => ns.map(n => n.textContent));
  check('занятия импорта исчезли из календаря, события остались', !left.some(t => t.includes('Занятие')) && left.includes('Утренняя встреча'), JSON.stringify(left));
  await page.click('[data-calendar-view="history"]'); await page.waitForTimeout(800);
  await page.locator('#historyScroll .history-item', { hasText: 'Утренняя встреча' }).locator('.history-delete').click();
  await page.waitForTimeout(1200);
  const afterEvent = await page.$$eval('#historyScroll .history-title', ns => ns.map(n => n.textContent));
  check('событие удалено из истории', !afterEvent.includes('Утренняя встреча'), JSON.stringify(afterEvent));

  // Expired session: the API redirects to re-auth.
  await context.clearCookies(); await page.evaluate(() => sessionStorage.clear());
  await page.goto(BASE + TIMELINE); await page.waitForTimeout(1500);
  check('без сессии — повторный вход', page.url().includes(TIMELINE) || page.url().includes(ENTRY), page.url());

  console.log('\nОшибки JS:', errors.length ? errors : 'нет');
  console.log('HTTP ≥400:', failed.length ? failed : 'нет');
  console.log(`\n${results.filter(r => r[1]).length}/${results.length} проверок мини-приложения прошло`);
  await browser.close();
})();
