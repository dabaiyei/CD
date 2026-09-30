import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';
let input = ''; for await (const chunk of process.stdin) input += chunk;
const {url, executablePath} = JSON.parse(input);
const browser = await puppeteer.launch({executablePath,headless:true});
try {
  const page = await browser.newPage();
  page.on('pageerror', error => console.log('PAGE_ERROR',error.message));
  page.on('requestfailed', request => console.log('REQUEST_FAILED',new URL(request.url()).pathname,request.failure()?.errorText));
  page.on('response', response => { if(response.status()>=400) console.log('HTTP_ERROR',response.status(),new URL(response.url()).pathname); });
  await page.goto(url,{waitUntil:'domcontentloaded',timeout:120000});
  await page.waitForSelector('.studio-shell',{timeout:60000});
  await page.waitForFunction(() => {
    const frame = document.querySelector('.stage iframe');
    return frame?.contentDocument?.querySelector('video')?.readyState >= 2;
  },{timeout:90000});
  console.log('VIDEO_READY',await page.$eval('.stage iframe',e=>{
    const v=e.contentDocument.querySelector('video');return {width:v.videoWidth,height:v.videoHeight,duration:v.duration};
  }));
  console.log('BODY', (await page.locator('body').waitHandle()).asElement() ? await page.$eval('body', e=>e.innerText.slice(0,1400)) : 'missing');
} finally { await browser.close(); }
