import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';
let input='';for await(const chunk of process.stdin)input+=chunk;
const {token,executablePath}=JSON.parse(input);
const browser=await puppeteer.launch({executablePath,headless:true});
try{
 const page=await browser.newPage();
 await page.setViewport({width:1366,height:900});
 page.on('pageerror',e=>console.log('PAGE_ERROR',e.message));
 await page.evaluateOnNewDocument(t=>localStorage.setItem('cineforge.access-token',t),token);
 await page.goto('http://127.0.0.1:5173/video-replicas',{waitUntil:'networkidle2'});
 await page.waitForFunction(()=>Array.from(document.querySelectorAll('.replica-tabs button')).some(e=>e.textContent.includes('制作工程')));
 await page.locator('.replica-tabs button:last-child').click();
 await page.waitForSelector('.cut-preview video',{timeout:15000,visible:true}).catch(async e=>{console.log('DIAGNOSTIC',await page.evaluate(()=>({text:document.body.innerText.slice(-14000),tabs:Array.from(document.querySelectorAll('.replica-tabs button')).map(e=>({text:e.textContent,pressed:e.getAttribute('aria-pressed')}))})));throw e});
 for(const width of [1366,390]){
  await page.setViewport({width,height:900});
  await page.screenshot({path:`C:/Code/CD/.logs/replica-cut-${width}.png`,fullPage:true});
  console.log('LAYOUT',width,await page.$eval('.cut-editor',e=>({width:e.getBoundingClientRect().width,scroll:e.scrollWidth,text:e.innerText.slice(0,600)})));
 }
 const move=await page.$('button[aria-label="后移片段"]');await move.click();
 const undo=await page.$$('.cut-footer button');await undo[0].click();
 console.log('UNDO',await page.$eval('.cut-footer',e=>e.innerText));
}finally{await browser.close()}
