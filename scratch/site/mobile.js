const { chromium } = require('playwright');
(async () => { const b = await chromium.launch();
  for (const scheme of ['light','dark']) {
    const p = await b.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: scheme, hasTouch: true });
    await p.goto('http://127.0.0.1:8077/', { waitUntil: 'networkidle' });
    for (const [i, y] of [[0, 0], [1, 700], [2, 1500], [3, 3200], [4, 99999]].entries()) {
      await p.evaluate(v => window.scrollTo(0, v), y[1]); await p.waitForTimeout(500);
      await p.screenshot({ path: `${__dirname}/shots/m_${scheme}_${i}.png` });
    }
    await p.goto('http://127.0.0.1:8077/login', { waitUntil: 'networkidle' });
    await p.screenshot({ path: `${__dirname}/shots/m_${scheme}_login.png` });
  }
  await b.close(); })();
