// Design checks of the public pages: layout, images, console, contrast, tap targets.
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8077';
const PAGES = (process.env.PAGES || '/,/login').split(',');
const VIEWPORTS = [
  ['360', { width: 360, height: 740 }], ['390', { width: 390, height: 844 }],
  ['768', { width: 768, height: 1024 }], ['1024', { width: 1024, height: 768 }],
  ['1440', { width: 1440, height: 900 }],
];
const problems = [];
let checks = 0;
const fail = (where, what) => problems.push(`${where}: ${what}`);

const audit = () => {
  const out = { overflow: 0, brokenImages: [], noAlt: [], h1: 0, contrast: [], small: [], offscreen: [] };
  out.overflow = document.documentElement.scrollWidth - window.innerWidth;
  for (const img of document.images) {
    if (!img.complete || img.naturalWidth === 0) out.brokenImages.push(img.getAttribute('src'));
    if (!img.hasAttribute('alt')) out.noAlt.push(img.getAttribute('src'));
  }
  out.h1 = document.querySelectorAll('h1').length;
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  const lum = ({ r, g, b }) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const background = el => {
    for (let node = el; node; node = node.parentElement) {
      const c = parse(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0.5) return c;
    }
    return parse(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255 };
  };
  const selector = 'h1,h2,h3,p,a,button,summary,.chip,.badge,.eyebrow,.bubble,.cta-note,small,footer span,.login-divider';
  for (const el of document.querySelectorAll(selector)) {
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height || getComputedStyle(el).visibility === 'hidden' || el.closest('[hidden]')) continue;
    if (!el.textContent.trim()) continue;
    const style = getComputedStyle(el);
    const fg = parse(style.color), bg = background(el);
    if (!fg) continue;
    const ratio = (Math.max(lum(fg), lum(bg)) + 0.05) / (Math.min(lum(fg), lum(bg)) + 0.05);
    const size = parseFloat(style.fontSize), bold = parseInt(style.fontWeight) >= 700;
    const need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio < need) out.contrast.push(`${el.tagName.toLowerCase()}.${el.className || ''} «${el.textContent.trim().slice(0, 30)}» ${ratio.toFixed(2)}<${need}`);
    if ((el.matches('button, .btn, summary, .nav a, .messenger-button')) && rect.height < 40) out.small.push(`«${el.textContent.trim().slice(0, 20)}» ${Math.round(rect.height)}px`);
    if (rect.right > window.innerWidth + 1 && style.position !== 'fixed') out.offscreen.push(`«${el.textContent.trim().slice(0, 20)}»`);
  }
  return out;
};

(async () => {
  const browser = await chromium.launch();
  for (const scheme of ['light', 'dark']) {
    for (const [vwName, viewport] of VIEWPORTS) {
      const context = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: scheme, locale: 'ru-RU', hasTouch: viewport.width < 800 });
      for (const path of PAGES) {
        const page = await context.newPage();
        const errors = [], bad = [];
        page.on('pageerror', e => errors.push(e.message));
        page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
        page.on('response', r => { if (r.status() >= 400) bad.push(`${r.status()} ${r.url()}`); });
        const where = `${path} ${vwName}px ${scheme}`;
        await page.goto(BASE + path, { waitUntil: 'networkidle' });
        // Lazy images load when scrolled into view.
        await page.evaluate(async () => { for (let y = 0; y < document.body.scrollHeight; y += 500) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 40)); } window.scrollTo(0, 0); });
        await page.evaluate(async () => {
          for (const img of document.images) {
            img.scrollIntoView({ block: 'center' });
            await Promise.race([img.decode().catch(() => {}), new Promise(done => setTimeout(done, 4000))]);
          }
          window.scrollTo(0, 0);
        });
        await page.waitForTimeout(200);
        const result = await page.evaluate(audit);
        checks += 1;
        if (result.overflow > 1) fail(where, `горизонтальная прокрутка на ${result.overflow}px`);
        if (result.brokenImages.length) fail(where, `не загрузились картинки ${result.brokenImages}`);
        if (result.noAlt.length) fail(where, `нет alt у ${result.noAlt}`);
        if (result.h1 !== 1) fail(where, `заголовков h1: ${result.h1}`);
        if (result.contrast.length) fail(where, `низкий контраст: ${result.contrast.slice(0, 6).join('; ')}`);
        if (result.small.length) fail(where, `мелкие кнопки: ${result.small.join(', ')}`);
        if (result.offscreen.length) fail(where, `за краем экрана: ${result.offscreen.join(', ')}`);
        if (errors.length) fail(where, `ошибки консоли: ${errors.join(' | ')}`);
        if (bad.length) fail(where, `HTTP ошибки: ${bad.join(', ')}`);
        const name = `${path === '/' ? 'home' : path.replace(/\W/g, '')}_${vwName}_${scheme}.png`;
        await page.screenshot({ path: `${__dirname}/shots/${name}`, fullPage: true });
        await page.close();
      }
      await context.close();
    }
  }
  await browser.close();
  console.log(problems.length ? problems.join('\n') : 'замечаний нет');
  console.log(`\nпроверено состояний: ${checks}, замечаний: ${problems.length}`);
})();
