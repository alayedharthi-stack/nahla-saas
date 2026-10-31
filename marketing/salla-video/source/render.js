// usage: node render.js <workers> <fps> <outdir>
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const [W = 4, FPS = 30, OUT = '/tmp/nahla-video/out'] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch();
  const probe = await b.newPage({ viewport: { width: 1080, height: 1920 } });
  await probe.goto('file:///tmp/nahla-video/src/index.html');
  await probe.evaluate(() => window.ready);
  const dur = await probe.evaluate(() => window.DURATION);
  await probe.close();
  const N = Math.round(dur * FPS);
  const per = Math.ceil(N / W);
  console.log('frames', N);
  await Promise.all([...Array(+W).keys()].map(async w => {
    const a = w * per, z = Math.min(N, a + per);
    const p = await b.newPage({ viewport: { width: 1080, height: 1920 } });
    p.on('pageerror', e => console.log('pageerror', e.message));
    await p.goto('file:///tmp/nahla-video/src/index.html');
    await p.evaluate(() => window.ready);
    const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-c:v', 'png', '-framerate', String(FPS), '-i', '-',
      '-c:v', 'libx264', '-preset', 'slow', '-crf', '17', '-pix_fmt', 'yuv420p', '-r', String(FPS), `${OUT}/seg${w}.mp4`], { stdio: ['pipe', 'inherit', 'inherit'] });
    for (let i = a; i < z; i++) {
      await p.evaluate(t => seek(t), i / FPS);
      const buf = await p.screenshot({ type: 'png' });
      if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
      if ((i - a) % 60 === 0) console.log(`w${w} ${i - a}/${z - a}`);
    }
    ff.stdin.end();
    await new Promise(r => ff.on('close', r));
  }));
  await b.close();
  console.log('done');
})();
