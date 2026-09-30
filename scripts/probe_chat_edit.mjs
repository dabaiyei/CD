import assert from 'node:assert/strict';
import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';
const browser = await puppeteer.launch({executablePath:`${process.env.LOCALAPPDATA}/ms-playwright/chromium-1228/chrome-win64/chrome.exe`,headless:true});
try {
  for (const width of [1366,390]) {
    const page = await browser.newPage();
    page.on('pageerror',error=>console.log(error.message));
    page.on('console',message=>{if(message.type()==='error') console.log(message.text());});
    await page.setViewport({width,height:900});
    await page.evaluateOnNewDocument(()=>localStorage.setItem('cineforge.access-token','ui-test'));
    const session={id:'s',title:'编辑测试会话',runtime_manifest:{mode:'chat'},created_at:new Date().toISOString(),last_message_at:new Date().toISOString()};
    let messages=['第一条问题','第一条回答','需要修改的问题','应该清除的旧回答'].map((content,i)=>({id:`m${i}`,session_id:'s',role:i%2?'assistant':'user',content,runtime_manifest:{},runtime_events:[],created_at:new Date(Date.now()+i).toISOString()}));
    let submitted;
    await page.setRequestInterception(true);
    page.on('request',req=>{
      const p=new URL(req.url()).pathname;
      if(!p.startsWith('/api/')) return req.continue();
      let data=[];
      if(p.endsWith('/auth/me')) data={user:{id:'u',role:'admin',display_name:'测试',background_blur:0},credit_balance:'100'};
      else if(p.endsWith('/tasks') || p.endsWith('/notifications')) data={items:[],next_before:null,unread_count:0};
      else if(p.endsWith('/projects/options')) data={text_models:[],image_models:[],video_models:[],tts_models:[],visual_handbooks:[],director_handbooks:[],styles:[],genres:[]};
      else if(p.endsWith('/agent/options')) data={agents:[{id:'a',name:'AI'}],skills:[],text_models:[],image_models:[],video_models:[],tts_models:[]};
      else if(p.endsWith('/agent/sessions')) data=[session];
      else if(p.endsWith('/agent/sessions/s')) data={session,messages,active_task:null};
      else if(p.endsWith('/messages') && req.method()==='POST') {
        submitted=JSON.parse(req.postData());messages=messages.slice(0,3);messages[2]={...messages[2],content:submitted.content};
        data={session,user_message:messages[2],task:{id:'task',status:'queued',task_type:'agent_chat_run',request_payload:{},created_at:new Date().toISOString()}};
      } else if(p.endsWith('/tasks/task')) data={id:'task',status:'queued',task_type:'agent_chat_run',request_payload:{}};
      req.respond({status:200,contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto('http://127.0.0.1:5173/workspace',{waitUntil:'networkidle0'});
    await page.waitForSelector('[title="打开历史记录"]');
    await page.click('[title="打开历史记录"]');
    await page.waitForSelector('.agent-history-entry__open');await page.click('.agent-history-entry__open');
    await page.waitForSelector('[aria-label="编辑此消息"]');
    const buttons=await page.$$('[aria-label="编辑此消息"]');await buttons[buttons.length-1].click();
    await page.waitForSelector('.agent-inline-editor textarea');
    assert.equal(await page.$eval('.agent-inline-editor textarea',el=>el.value),'需要修改的问题');
    await page.$eval('.agent-inline-editor textarea',el=>{el.value='修改后的问题';el.dispatchEvent(new Event('input',{bubbles:true}));});
    await page.screenshot({path:`.logs/chat-inline-edit-${width}.png`,fullPage:true});
    await page.click('.agent-inline-editor button[type=submit]');
    await page.waitForFunction(()=>!document.querySelector('.agent-inline-editor'));
    assert.equal(submitted.edit_message_id,'m2');assert.equal(submitted.content,'修改后的问题');
    assert.equal(await page.evaluate(()=>document.body.textContent.includes('应该清除的旧回答')),false);
    console.log(`${width}px: inline edit and branch truncation passed`);
    await page.close();
  }
} finally { await browser.close(); }
