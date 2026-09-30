import assert from 'node:assert/strict';
import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';

const browser = await puppeteer.launch({ executablePath: `${process.env.LOCALAPPDATA}/ms-playwright/chromium-1228/chrome-win64/chrome.exe`, headless: true });
try {
  const page = await browser.newPage();
  await page.evaluateOnNewDocument(() => localStorage.setItem('cineforge.access-token', 'ui-test'));
  const platforms = [
    {provider:'typesafe', name:'Typesafe', has_api_key:true, model:'jev-1.13.0', default_model:'jev-1.13.0', source:'environment', endpoint:'https://api.typesafe.ai/v1/systemone'},
    {provider:'opencode_zen', name:'OpenCode Zen', has_api_key:false, model:'jev-1.13-free', default_model:'jev-1.13-free', source:'database', endpoint:'https://opencode.ai/zen/v1/systemone'},
  ];
  let config = {enabled: true, provider:'typesafe', has_api_key: true, model: 'jev-1.13.0', timeout_seconds: 8, route_confidence: .65, source: 'environment', endpoint: 'https://api.typesafe.ai/v1/systemone', platforms};
  await page.setRequestInterception(true);
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return request.continue();
    let value = {};
    if (path.endsWith('/auth/me')) value = {user: {id:'ui', tenant_id:'ui', display_name:'管理员', role:'admin', background_blur:0}, credit_balance:'1000'};
    else if (path.endsWith('/admin/jev/test')) value = {ok:true, model:config.model, latency_ms:800};
    else if (path.endsWith('/admin/jev')) {
      if (request.method() === 'PUT') {
        const payload = JSON.parse(request.postData());
        const selected = platforms.find(p => p.provider === payload.provider);
        assert.ok(selected);
        selected.model = payload.model;
        if (payload.api_key) selected.has_api_key = true;
        if (payload.clear_api_key) selected.has_api_key = false;
        config = {...config, ...payload, endpoint:selected.endpoint, has_api_key:selected.has_api_key, source:'database'};
        delete config.api_key;
      }
      value = config;
    } else if (path.endsWith('/notifications')) value = [];
    request.respond({status:200, contentType:'application/json', body:JSON.stringify(value)});
  });
  for (const width of [1366, 390]) {
    await page.setViewport({width, height:900});
    await page.goto('http://127.0.0.1:5173/admin/jev', {waitUntil:'networkidle0'});
    await page.waitForSelector('.jev-form');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.$eval('input[type=password]', el => el.value), '');
    const outside = await page.$$eval('.jev-form input, .jev-form select, .jev-actions button', elements => elements.some(el => {const r=el.getBoundingClientRect();return r.left < 0 || r.right > innerWidth;}));
    assert.equal(outside, false);
    await page.screenshot({path:`.logs/jev-admin-${width}.png`, fullPage:true});
    console.log(`JEV admin ${width}px: no overflow, masked key, controls visible`);
    await page.select('.jev-grid select', 'opencode_zen');
    assert.equal(await page.$eval('input[list=jev-models]', el => el.value), 'jev-1.13-free');
    assert.equal(await page.$eval('input[type=password]', el => el.value), '');
    assert.match(await page.$eval('.jev-endpoint', el => el.textContent), /opencode.ai\/zen/);
    assert.equal(await page.$eval('.jev-actions button[type=button]', el => el.disabled), true);
    await page.select('.jev-grid select', 'typesafe');
    assert.equal(await page.$eval('input[list=jev-models]', el => el.value), 'jev-1.13.0');
  }
  await page.click('.jev-actions button[type=button]');
  await page.waitForSelector('.jev-result');
  assert.match(await page.$eval('.jev-result', el=>el.textContent), /800 ms/);
  await page.click('input[role=switch]');
  await page.click('.jev-actions button[type=submit]');
  await page.waitForFunction(()=>document.querySelector('.jev-status')?.textContent === '未启用');
  console.log('Test-connection and save-state UI passed');
  await page.select('.jev-grid select', 'opencode_zen');
  await page.type('input[type=password]', 'ui-only-fake-key');
  await page.click('.jev-actions button[type=submit]');
  await page.waitForFunction(()=>document.querySelector('input[type=password]')?.value === '' && !document.querySelector('.jev-actions button[type=button]')?.disabled);
  assert.equal(config.provider, 'opencode_zen');
  await page.click('.jev-actions button[type=button]');
  await page.waitForFunction(()=>document.querySelector('.jev-result')?.textContent.includes('jev-1.13-free'));
  console.log('Zen platform switching, isolated credential field, save and test passed');
} finally { await browser.close(); }
