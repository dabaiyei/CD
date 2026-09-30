import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { stripTypeScriptTypes } from 'node:module';
import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';

const source = stripTypeScriptTypes(await readFile(new URL('../apps/web/src/lib/clipboard.ts', import.meta.url), 'utf8'));
const server = createServer((req, res) => {
  res.setHeader('Content-Type', req.url === '/clipboard.js' ? 'text/javascript' : 'text/html; charset=utf-8');
  res.end(req.url === '/clipboard.js' ? source : `<!doctype html><button id="copy">Copy</button><textarea id="paste"></textarea><dialog><button id="modalCopy">Copy in dialog</button></dialog><script type="module">import {copyText} from '/clipboard.js'; window.ready=true; for(const id of ['copy','modalCopy']) document.getElementById(id).onclick=async()=>{window.result=await copyText('HTTP复制验证\\n中文、emoji ✅');};</script>`);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await puppeteer.launch({
  executablePath: `${process.env.LOCALAPPDATA}/ms-playwright/chromium-1228/chrome-win64/chrome.exe`,
  headless: true, args: ['--host-resolver-rules=MAP clipboard.test 127.0.0.1', '--no-proxy-server'],
});
try {
  const page = await browser.newPage();
  await page.goto(`http://clipboard.test:${server.address().port}`);
  await page.waitForFunction(() => window.ready);
  assert.equal(await page.evaluate(() => isSecureContext), false);
  assert.equal(await page.evaluate(() => !!navigator.clipboard), false);
  for (const modal of [false, true]) {
    await page.evaluate(modal => { window.result=null; if(modal) document.querySelector('dialog').showModal(); }, modal);
    await page.click(modal ? '#modalCopy' : '#copy');
    await page.waitForFunction(() => window.result !== null);
    assert.equal(await page.evaluate(() => window.result), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), modal ? 'modalCopy' : 'copy');
    if (modal) await page.evaluate(() => document.querySelector('dialog').close());
    await page.$eval('#paste', el => {el.value='';el.focus();});
    await page.keyboard.down('Control'); await page.keyboard.press('V'); await page.keyboard.up('Control');
    await page.waitForFunction(() => document.querySelector('#paste').value.includes('✅'));
    assert.equal(await page.$eval('#paste', el => el.value), 'HTTP复制验证\n中文、emoji ✅');
    console.log(modal ? 'HTTP modal copy + actual paste passed' : 'HTTP copy + actual paste passed');
  }
  await page.evaluate(() => {document.execCommand=()=>false; window.result=null;});
  await page.click('#copy'); await page.waitForFunction(() => window.result !== null);
  assert.equal(await page.evaluate(() => window.result), false);
  console.log('Denied copy does not report success');
} finally { await browser.close(); server.close(); }
