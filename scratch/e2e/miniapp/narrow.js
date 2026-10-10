// A narrow window with a mouse (Telegram Desktop): day view, mouse editing.
const { chromium } = require('playwright');
const fs = require('fs');
const initData = fs.readFileSync(__dirname + '/initdata.txt', 'utf8').trim();
(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 420, height: 760 }, locale: 'ru-RU', timezoneId: 'Europe/Moscow' })).newPage();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.route('https://telegram.org/js/telegram-web-app.js', r => r.fulfill({ contentType: 'application/javascript', body:
    `window.Telegram={WebApp:{initData:${JSON.stringify(initData)},platform:'tdesktop',version:'8.0',ready(){},expand(){},sendData(){},themeParams:{},colorScheme:'light'}};` }));
  await page.goto('http://127.0.0.1:8077/miniapp'); await page.waitForURL(/mini-timeline/); await page.waitForTimeout(1000);
  const out = {};
  out.weekButtonHidden = !(await page.isVisible('[data-calendar-view="week"]'));
  out.dayView = await page.locator('#timeline .events-layer').count() === 1;
  await page.keyboard.press('KeyJ'); await page.waitForTimeout(300);
  await page.evaluate(() => { document.getElementById('timelineScroll').scrollTop = document.querySelector('.time-grid').offsetTop + 8 * 60; });
  const box = await page.locator('#timeline .events-layer').boundingBox();
  await page.mouse.move(box.x + 100, box.y + 9 * 60 + 2); await page.mouse.down();
  for (let i = 1; i <= 6; i++) await page.mouse.move(box.x + 100, box.y + 9 * 60 + 2 + i * 10);
  await page.mouse.up(); await page.waitForTimeout(200);
  out.quick = await page.textContent('#quickWhen');
  const pop = await page.locator('#quickCreate').boundingBox();
  out.popoverInside = pop.x >= 0 && pop.x + pop.width <= 420 && pop.y >= 0 && pop.y + pop.height <= 760;
  await page.screenshot({ path: __dirname + '/desk_narrow.png' });
  await page.keyboard.type('Узкое окно'); await page.keyboard.press('Enter'); await page.waitForTimeout(900);
  out.saved = await page.locator('#timeline .calendar-event', { hasText: 'Узкое окно' }).count();
  out.errors = errors;
  console.log(JSON.stringify(out, null, 1));
  await browser.close();
})();
