// Desktop (mouse) calendar: week view, drag to create/move/resize, cards, shortcuts.
const { chromium } = require('playwright');
const fs = require('fs');
const MAX = process.env.PLATFORM === 'max', WEB = process.env.PLATFORM === 'web';
const BASE = MAX ? 'http://127.0.0.1:8078' : 'http://127.0.0.1:8077';
const ENTRY = MAX ? '/max/miniapp' : WEB ? '/calendar' : '/miniapp';
const TIMELINE = MAX ? '/max/timeline' : WEB ? '/calendar' : '/mini-timeline';
const initData = fs.readFileSync(__dirname + '/initdata.txt', 'utf8').trim();
const results = [];
const check = (name, ok, detail = '') => { results.push([name, !!ok]); console.log(`${ok ? '✔' : '✘'} ${name}${!ok && detail ? ' — ' + detail : ''}`); };
const shot = (page, name) => page.screenshot({ path: `${__dirname}/${MAX ? 'max_' : WEB ? 'web_' : ''}desk_${name}.png` });

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'ru-RU', timezoneId: 'Europe/Moscow', colorScheme: process.env.SCHEME || 'light' });
  if (WEB) await context.addCookies([{ name: 'planiruy_session', value: process.env.SESSION, url: BASE }]);
  const page = await context.newPage();
  const errors = [], failed = [], dialogs = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  page.on('response', r => { if (r.status() >= 400) failed.push(`${r.status()} ${r.request().method()} ${r.url()}`); });
  page.on('dialog', async d => { dialogs.push(d.message()); await d.accept(); });
  await page.route('https://telegram.org/js/telegram-web-app.js', route => route.fulfill({ contentType: 'application/javascript', body:
    `window.Telegram={WebApp:{initData:${JSON.stringify(initData)},platform:'tdesktop',version:'8.0',ready(){},expand(){},sendData(){},themeParams:{},colorScheme:'light'}};` }));
  await page.route('https://st.max.ru/js/max-web-app.js', route => route.fulfill({ contentType: 'application/javascript', body:
    `window.WebApp={initData:${JSON.stringify(initData)},platform:'web',ready(){},expand(){}};` }));

  await page.goto(BASE + ENTRY);
  await page.waitForURL(new RegExp(TIMELINE), { timeout: 10000 }).catch(() => {});
  await page.evaluate(() => { try { localStorage.removeItem('planiruy_view'); } catch (_) {} });
  await page.goto(BASE + TIMELINE); await page.waitForTimeout(1200);

  const layers = () => page.locator('#timeline .events-layer');
  const eventIn = (index, title) => layers().nth(index).locator('.calendar-event:not(.draft-event):not(.drag-preview)', { hasText: title });
  // Puts 08:00 right under the week header, then gives page Y for a minute of the day.
  async function scrollTo8() {
    await page.evaluate(() => {
      const area = document.getElementById('timelineScroll'), grid = document.querySelector('.time-grid');
      const head = area.querySelector('.week-head, .all-day-lane');
      area.scrollTop = grid.offsetTop + 8 * 60 - (head ? head.offsetHeight : 0);
    });
    await page.waitForTimeout(100);
  }
  async function slot(index, minutes) {
    const box = await layers().nth(index).boundingBox();
    return { x: box.x + box.width / 2, y: box.y + minutes, width: box.width };
  }
  async function drag(from, to, steps = 8) {
    await page.mouse.move(from.x, from.y); await page.mouse.down();
    for (let i = 1; i <= steps; i++) { await page.mouse.move(from.x + (to.x - from.x) * i / steps, from.y + (to.y - from.y) * i / steps); await page.waitForTimeout(20); }
    await page.mouse.up(); await page.waitForTimeout(150);
  }
  const timeOf = async (index, title) => (await eventIn(index, title).locator('.event-time').textContent()).trim();

  check('на широком экране с мышью сразу открывается неделя', await page.locator('.calendar-shell.week-mode').count() === 1 && await layers().count() === 7);
  check('в шапке — диапазон недели и стрелки', /–/.test(await page.textContent('#dateLabel')) && await page.isVisible('#prevButton') && await page.isVisible('#nextButton'), await page.textContent('#dateLabel'));
  check('сегодняшний день недели выделен', await page.locator('.week-name.today').count() === 1);
  await shot(page, '01_week');

  // Next week is empty apart from the seeded trip and the weekly class.
  const label0 = await page.textContent('#dateLabel');
  await page.click('#nextButton'); await page.waitForTimeout(400);
  check('стрелка «›» листает на неделю', (await page.textContent('#dateLabel')) !== label0, await page.textContent('#dateLabel'));
  const trip = page.locator('.week-all-day-grid .all-day-event', { hasText: 'Командировка' });
  check('событие на несколько дней растянуто в строке «весь день»', await trip.count() === 1 && /2 \/ 4/.test(await trip.getAttribute('style')), await trip.count() && await trip.getAttribute('style'));
  check('занятие из расписания стоит в своём дне', await layers().nth(6).locator('.schedule-event', { hasText: 'Матанализ' }).count() === 1);
  await scrollTo8();

  // Drag over empty time → quick card → Enter.
  await drag(await slot(2, 10 * 60 + 2), await slot(2, 11 * 60 + 30));
  check('протягивание по пустому месту открывает быстрое создание', await page.isVisible('#quickCreate'), '');
  check('время в карточке — как протянули (10:00–11:30)', /10:00–11:30/.test(await page.textContent('#quickWhen')), await page.textContent('#quickWhen'));
  check('курсор сразу в поле названия', await page.evaluate(() => document.activeElement.id) === 'quickTitle');
  await page.keyboard.type('Планёрка');
  check('название видно в черновике на сетке', await page.locator('.draft-event', { hasText: 'Планёрка' }).count() === 1);
  await shot(page, '02_quick');
  await page.keyboard.press('Enter'); await page.waitForTimeout(1000);
  check('Enter сохраняет событие в нужный день и время', await eventIn(2, 'Планёрка').count() === 1 && await timeOf(2, 'Планёрка') === '10:00–11:30', await eventIn(2, 'Планёрка').count());
  check('карточка закрылась, черновика нет', !(await page.isVisible('#quickCreate')) && await page.locator('.draft-event').count() === 0);

  // A click → one hour from the half hour; Esc discards.
  await page.mouse.click((await slot(3, 15 * 60 + 10)).x, (await slot(3, 15 * 60 + 10)).y); await page.waitForTimeout(200);
  check('клик по пустому месту — событие на час (15:00–16:00)', /15:00–16:00/.test(await page.textContent('#quickWhen')), await page.textContent('#quickWhen'));
  await page.keyboard.press('Escape'); await page.waitForTimeout(150);
  check('Esc закрывает карточку без сохранения', !(await page.isVisible('#quickCreate')) && await page.locator('.draft-event').count() === 0);

  // «Другие параметры» carries title and time into the full form.
  const s15 = await slot(3, 15 * 60 + 10);
  await page.mouse.click(s15.x, s15.y); await page.waitForTimeout(150);
  await page.keyboard.type('Подробно');
  await page.click('#quickMore'); await page.waitForTimeout(200);
  const fullForm = [await page.inputValue('#title'), await page.inputValue('#start-at'), await page.inputValue('#end-at')];
  check('«Другие параметры» открывают полную форму с теми же данными', await page.isVisible('#createModal.open') && fullForm[0] === 'Подробно' && fullForm[1].endsWith('T15:00') && fullForm[2].endsWith('T16:00'), JSON.stringify(fullForm));
  await page.keyboard.press('Escape'); await page.waitForTimeout(150);
  check('Esc закрывает и полную форму', !(await page.isVisible('#createModal.open')));

  // Clicking empty space while a card is open only closes the card.
  await page.mouse.click(s15.x, s15.y); await page.waitForTimeout(150);
  await page.mouse.click((await slot(5, 9 * 60)).x, (await slot(5, 9 * 60)).y); await page.waitForTimeout(150);
  check('клик мимо открытой карточки только закрывает её', !(await page.isVisible('#quickCreate')));

  // Move to the next day and an hour later, then undo.
  const ev = await eventIn(2, 'Планёрка').boundingBox();
  const from = { x: ev.x + ev.width / 2, y: ev.y + 15 };
  await drag(from, { x: from.x + (await slot(3, 0)).width, y: from.y + 60 }, 10);
  await page.waitForTimeout(1000);
  check('перетаскивание переносит в другой день и время', await eventIn(3, 'Планёрка').count() === 1 && await timeOf(3, 'Планёрка') === '11:00–12:30', `${await eventIn(3, 'Планёрка').count()} ${await page.textContent('#toast')}`);
  check('после переноса — «Отменить» в уведомлении', /Мероприятие перенесено/.test(await page.textContent('#toast')) && await page.isVisible('.toast-action'));
  await page.click('.toast-action'); await page.waitForTimeout(1000);
  check('«Отменить» возвращает как было', await eventIn(2, 'Планёрка').count() === 1 && await timeOf(2, 'Планёрка') === '10:00–11:30', await page.textContent('#toast'));

  // Resize the bottom edge by 30 minutes.
  const handle = await eventIn(2, 'Планёрка').locator('.resize-handle').boundingBox();
  await drag({ x: handle.x + handle.width / 2, y: handle.y + handle.height / 2 }, { x: handle.x + handle.width / 2, y: handle.y + handle.height / 2 + 30 });
  await page.waitForTimeout(1000);
  check('нижний край растягивает событие (до 12:00)', await timeOf(2, 'Планёрка') === '10:00–12:00', await timeOf(2, 'Планёрка'));

  // Card: details, edit, complete, delete.
  await eventIn(2, 'Планёрка').click(); await page.waitForTimeout(200);
  check('клик по событию открывает карточку с деталями', await page.isVisible('#eventDetails') && (await page.textContent('#detailsTitle')) === 'Планёрка' && /10:00–12:00/.test(await page.textContent('#detailsWhen')), await page.textContent('#detailsWhen'));
  await shot(page, '03_details');
  await page.click('#detailsEdit'); await page.waitForTimeout(200);
  check('кнопка-карандаш открывает полное редактирование', await page.isVisible('#editModal.open') && (await page.inputValue('#editTitle')) === 'Планёрка');
  await page.keyboard.press('Escape'); await page.waitForTimeout(150);
  await eventIn(2, 'Планёрка').dblclick(); await page.waitForTimeout(200);
  check('двойной клик по событию — сразу редактирование', await page.isVisible('#editModal.open') && !(await page.isVisible('#eventDetails')));
  await page.fill('#editTitle', 'Планёрка команды'); await page.click('#editSave'); await page.waitForTimeout(1000);
  check('правка из формы сохраняется', await eventIn(2, 'Планёрка команды').count() === 1);
  await eventIn(2, 'Планёрка команды').click(); await page.waitForTimeout(200);
  await page.click('#detailsComplete'); await page.waitForTimeout(1000);
  check('«Выполнено» из карточки', await layers().nth(2).locator('.calendar-event.done', { hasText: 'Планёрка команды' }).count() === 1);
  await eventIn(2, 'Планёрка команды').click(); await page.waitForTimeout(200);
  const asked = dialogs.length;
  await page.keyboard.press('Delete'); await page.waitForTimeout(1000);
  check('Delete на открытой карточке удаляет (с подтверждением)', (dialogs[asked] || '').includes('Планёрка команды') && await eventIn(2, 'Планёрка команды').count() === 0, dialogs[asked]);

  // All-day row: a click on an empty day.
  const grid = await page.locator('.week-all-day-grid').boundingBox();
  await page.mouse.click(grid.x + grid.width * (4.5 / 7), grid.y + grid.height - 6); await page.waitForTimeout(200);
  check('клик по строке «весь день» — событие на весь день', await page.isVisible('#quickCreate') && /весь день/.test(await page.textContent('#quickWhen')), await page.textContent('#quickWhen'));
  await page.keyboard.type('Отгул'); await page.keyboard.press('Enter'); await page.waitForTimeout(1000);
  const offDay = page.locator('.week-all-day-grid .all-day-event', { hasText: 'Отгул' });
  check('событие на весь день встало в свой день', await offDay.count() === 1 && /5 \/ 6/.test(await offDay.getAttribute('style')), await offDay.count() && await offDay.getAttribute('style'));

  // Shortcuts and the day view.
  await page.keyboard.press('KeyD'); await page.waitForTimeout(300);
  check('клавиша D — вид «день»', await page.locator('.calendar-shell.week-mode').count() === 0 && await layers().count() === 1);
  await page.keyboard.press('KeyW'); await page.waitForTimeout(300);
  check('клавиша W — снова неделя', await layers().count() === 7);
  const weekLabel = await page.textContent('#dateLabel');
  await page.keyboard.press('KeyK'); await page.waitForTimeout(200);
  check('клавиша K — предыдущая неделя', (await page.textContent('#dateLabel')) !== weekLabel);
  await page.keyboard.press('KeyT'); await page.waitForTimeout(200);
  check('клавиша T — сегодня', await page.locator('.week-name.today').count() === 1);
  await page.keyboard.press('KeyC'); await page.waitForTimeout(200);
  check('клавиша C — новое мероприятие', await page.isVisible('#createModal.open'));
  await page.keyboard.press('Escape');

  // A day name opens that day; editing works there too.
  await page.click('#nextButton'); await page.waitForTimeout(300);
  await page.locator('.week-name').nth(1).click(); await page.waitForTimeout(400);
  check('клик по дню в шапке недели открывает этот день', await layers().count() === 1 && /вторник/i.test(await page.textContent('#dateLabel')), await page.textContent('#dateLabel'));
  await scrollTo8();
  await drag(await slot(0, 13 * 60 + 2), await slot(0, 14 * 60));
  await page.keyboard.type('Обед'); await page.click('#quickSave'); await page.waitForTimeout(1000);
  check('в виде «день» тоже создаётся протягиванием', await eventIn(0, 'Обед').count() === 1 && await timeOf(0, 'Обед') === '13:00–14:00');
  await shot(page, '04_day');

  // The choice of view is remembered.
  await page.reload(); await page.waitForTimeout(1000);
  check('выбранный вид запоминается', await layers().count() === 1);
  await page.keyboard.press('KeyW'); await page.waitForTimeout(300);

  console.log('\nОшибки JS:', errors.length ? errors : 'нет');
  console.log('HTTP ≥400:', failed.length ? failed : 'нет');
  console.log(`\n${results.filter(r => r[1]).length}/${results.length} проверок десктопа прошло`);
  await browser.close();
})();
