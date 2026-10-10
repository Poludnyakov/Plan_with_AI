// Design audit of the calendar itself: every view and window, phone to desktop, light and dark.
const { chromium } = require('playwright');
const fs = require('fs');
const P = process.env.PLATFORM || 'telegram';
const MAX = P === 'max';
const BASE = MAX ? 'http://127.0.0.1:8078' : 'http://127.0.0.1:8077';
const ENTRY = MAX ? '/max/miniapp' : '/miniapp';
const TIMELINE = MAX ? '/max/timeline' : '/mini-timeline';
const initData = fs.readFileSync(__dirname + '/initdata.txt', 'utf8').trim();
const SHOTS = __dirname + '/design_shots';
fs.mkdirSync(SHOTS, { recursive: true });
const VIEWPORTS = [
  ['360', { width: 360, height: 740 }, true], ['390', { width: 390, height: 844 }, true],
  ['768', { width: 768, height: 1024 }, true], ['1024', { width: 1024, height: 768 }, false],
  ['1440', { width: 1440, height: 900 }, false],
];
const problems = [];
const grouped = new Map();  // issue → states where it shows
let states = 0;

const audit = () => {
  const out = { overflow: document.documentElement.scrollWidth - innerWidth, broken: [], contrast: [], small: [], offscreen: [] };
  for (const img of document.images) if (img.offsetParent && (!img.complete || !img.naturalWidth)) out.broken.push(img.getAttribute('src'));
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = ({ r, g, b }) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const background = el => { for (let n = el; n; n = n.parentElement) { const c = parse(getComputedStyle(n).backgroundColor); if (c && c.a > 0.5) return c; } return { r: 255, g: 255, b: 255 }; };
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width && r.height && s.visibility !== 'hidden' && !el.closest('[hidden]') && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight; };
  // Faded on purpose: schedule classes, done and past items.
  const faded = el => el.closest('.schedule-event,.done,.history-item.undone,.drag-origin,.week-day,[disabled],.modal-backdrop:not(.open)');
  for (const el of document.querySelectorAll('h1,h2,h3,p,a,button,label,small,.event-title,.event-time,.agenda-title,.agenda-time,.history-title,.history-detail,.popover-when,.hour-label,.toast,.week-name small')) {
    if (!visible(el) || !el.textContent.trim() || faded(el)) continue;
    const s = getComputedStyle(el); let opacity = 1;
    for (let n = el; n; n = n.parentElement) opacity *= parseFloat(getComputedStyle(n).opacity);
    if (opacity < 0.95) continue;
    const fg = parse(s.color), bg = background(el); if (!fg) continue;
    const ratio = (Math.max(lum(fg), lum(bg)) + 0.05) / (Math.min(lum(fg), lum(bg)) + 0.05);
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight) >= 700;
    const need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio < need) out.contrast.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]} «${el.textContent.trim().slice(0, 24)}» ${ratio.toFixed(2)}`);
  }
  for (const el of document.querySelectorAll('button,a,input,select,textarea')) {
    if (!visible(el) || el.closest('.week-day,.calendar-event,.all-day-event,.agenda-event,.resize-handle,.week-all-day-grid') || el.type === 'checkbox' || el.classList.contains('date-picker')) continue;
    const r = el.getBoundingClientRect();
    if (Math.min(r.height, r.width) < 32) out.small.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]} «${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 16)}» ${Math.round(r.width)}×${Math.round(r.height)}`);
    if (r.right > innerWidth + 1 || r.left < -1) out.offscreen.push(`«${(el.textContent || '').trim().slice(0, 16)}»`);
  }
  return out;
};

async function inspect(page, where) {
  await page.waitForTimeout(250);
  const r = await page.evaluate(audit);
  states += 1;
  const add = (what, list) => { for (const item of new Set(list)) { const key = `${what}: ${item.replace(/\s+/g, ' ')}`; if (!grouped.has(key)) grouped.set(key, []); grouped.get(key).push(where); } };
  if (r.overflow > 1) add('горизонтальная прокрутка', [`${r.overflow}px`]);
  add('картинки', r.broken); add('контраст', r.contrast); add('мелкие кнопки', r.small); add('за краем', r.offscreen);
  await page.screenshot({ path: `${SHOTS}/${where.replace(/[^\wа-я]+/gi, '_')}.png` });
}

(async () => {
  const browser = await chromium.launch();
  for (const scheme of ['light', 'dark']) {
    for (const [name, viewport, touch] of VIEWPORTS) {
      const context = await browser.newContext({ viewport, hasTouch: touch, colorScheme: scheme, locale: 'ru-RU', timezoneId: 'Europe/Moscow' });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('dialog', d => d.dismiss());
      await page.route('https://telegram.org/js/telegram-web-app.js', r => r.fulfill({ contentType: 'application/javascript', body:
        `window.Telegram={WebApp:{initData:${JSON.stringify(initData)},platform:'android',version:'8.0',ready(){},expand(){},sendData(){},themeParams:{},colorScheme:'${scheme}'}};` }));
      await page.route('https://st.max.ru/js/max-web-app.js', r => r.fulfill({ contentType: 'application/javascript', body:
        `window.WebApp={initData:${JSON.stringify(initData)},platform:'android',ready(){},expand(){}};` }));
      await page.goto(BASE + ENTRY); await page.waitForURL(new RegExp(TIMELINE), { timeout: 10000 }).catch(() => {});
      await page.evaluate(() => { try { localStorage.removeItem('planiruy_view'); } catch (_) {} });
      await page.goto(BASE + TIMELINE + '?view=agenda'); await page.waitForTimeout(900);
      const tag = `${P}_${name}_${scheme}`;
      await inspect(page, `${tag}_расписание`);
      // Tomorrow has the seeded events.
      await page.click('[data-calendar-view="timeline"]');
      const t = new Date(); t.setDate(t.getDate() + 1);
      const key = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
      await page.evaluate(k => { const b = document.querySelector(`.week-day[data-day="${k}"]`); if (b) b.click(); }, key);
      await page.waitForTimeout(300);
      await page.evaluate(() => { const s = document.getElementById('timelineScroll'); s.scrollTop = document.querySelector('.time-grid').offsetTop + 9 * 60 - 40; });
      await inspect(page, `${tag}_день`);
      if (viewport.width >= 900) {
        await page.click('[data-calendar-view="week"]'); await page.waitForTimeout(300);
        await page.evaluate(() => { const s = document.getElementById('timelineScroll'); s.scrollTop = document.querySelector('.time-grid').offsetTop + 9 * 60 - 40; });
        await inspect(page, `${tag}_неделя`);
        const ev = page.locator('#timeline .calendar-event:not(.schedule-event)').first();
        if (await ev.count()) { await ev.click(); await inspect(page, `${tag}_карточка`); await page.keyboard.press('Escape'); }
        const layer = await page.locator('#timeline .events-layer').nth(3).boundingBox();
        await page.mouse.click(layer.x + layer.width / 2, layer.y + 16 * 60 + 10);
        await inspect(page, `${tag}_быстрое_создание`); await page.keyboard.press('Escape');
      } else {
        const ev = page.locator('#timeline .calendar-event:not(.schedule-event)').first();
        if (await ev.count()) { await ev.click(); await page.waitForTimeout(150); await ev.click(); await inspect(page, `${tag}_редактирование`); await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open'))); }
      }
      await page.click('#addEventButton'); await inspect(page, `${tag}_создание`);
      await page.evaluate(() => document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open')));
      await page.click('[data-calendar-view="history"]'); await page.waitForTimeout(700);
      await inspect(page, `${tag}_история`);
      await page.click('.import-open');
      for (const kind of ['file', 'link', 'caldav']) { await page.click(`[data-import-kind="${kind}"]`); await inspect(page, `${tag}_импорт_${kind}`); }
      if (errors.length) problems.push(`${tag}: ошибки JS: ${errors.join(' | ')}`);
      await context.close();
    }
  }
  await browser.close();
  for (const [key, where] of grouped) {
    const themes = [...new Set(where.map(w => w.split('_')[2]))].join('+');
    const widths = [...new Set(where.map(w => w.split('_')[1]))].join(',');
    console.log(`• ${key}  [${where.length} сост.; ${themes}; ${widths}px]`);
  }
  for (const p of problems) console.log('• ' + p);
  console.log(`\nпроверено состояний: ${states}, разных замечаний: ${grouped.size + problems.length}`);
})();
