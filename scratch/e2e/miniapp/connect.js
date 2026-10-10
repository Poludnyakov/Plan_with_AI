const { chromium } = require('playwright');
(async () => { const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 390, height: 760 }, deviceScaleFactor: 2 });
const errs=[]; p.on('pageerror', e => errs.push(e.message));
await p.goto('http://127.0.0.1:8077/calendar/connect/TRyxJ5stEh8wEa6z4uizxo9v7O_kNzQn'); await p.screenshot({ path: __dirname + '/connect.png' });
console.log('errors', errs.length, 'links', await p.$$eval('a.button', a => a.map(x => x.getAttribute('href').slice(0, 40)))); await b.close(); })();
