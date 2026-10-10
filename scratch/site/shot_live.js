const { chromium } = require('playwright');
(async () => { const b = await chromium.launch();
  for (const [name, vw] of [['live_login_mobile', {width:390,height:844}], ['live_login_desktop', {width:1366,height:820}]]) {
    const p = await b.newPage({ viewport: vw, deviceScaleFactor: 1 });
    await p.goto('https://planwithai.ru/login', { waitUntil: 'networkidle' }); await p.screenshot({ path: __dirname + '/' + name + '.png' }); }
  await b.close(); })();
