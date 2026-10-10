const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1000, height: 1400 }, deviceScaleFactor: 2 });
  await p.goto('file://' + __dirname + '/tables.html');
  for (const id of ['school', 'sport', 'uni']) await (await p.$('#' + id)).screenshot({ path: __dirname + '/' + id + '.png' });
  await b.close();
})();
