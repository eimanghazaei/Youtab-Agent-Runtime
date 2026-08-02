import { chromium } from 'playwright';
const CHROME='/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const b = await chromium.launch({ executablePath: CHROME, args:['--no-sandbox'] });
const p = await b.newPage({ viewport:{width:879,height:1100}, deviceScaleFactor:1 });
await p.goto('file://'+process.env.SP+'/unbroker.html', {waitUntil:'load'});
await p.waitForTimeout(1200);
await p.locator('.fig').screenshot({ path: process.env.SP+'/shots/unbroker.png' });
console.log('rendered');
await b.close();
