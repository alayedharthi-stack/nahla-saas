// usage: node snap.js out_dir t1 t2 ...
const { chromium } = require('playwright');
(async () => {
  const [out, ...ts] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: +(process.env.VW || 1080), height: +(process.env.VH || 1920) } });
  p.on('console', m => console.log('console:', m.text()));
  p.on('pageerror', e => console.log('pageerror:', e.message));
  await p.goto('file:///tmp/nahla-video/src/' + (process.env.PAGE || 'index.html') + '');
  await p.evaluate(() => window.ready);
  for (const t of ts) {
    await p.evaluate(t => seek(t), +t);
    await p.screenshot({ path: `${out}/t${t}.png` });
  }
  await b.close();
})();
