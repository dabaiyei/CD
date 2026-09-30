import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';

// Test the production Vue + React module graph, without modifying user data or calling a model.
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8WQAAAAASUVORK5CYII=', 'base64');
const videoFixture = execFileSync(process.platform==='win32'?'.venv/Scripts/ffmpeg.exe':'ffmpeg',[
    '-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=96x64:r=12:d=2',
    '-c:v','libx264','-pix_fmt','yuv420p','-f','mp4','-movflags','frag_keyframe+empty_moov','pipe:1'
]);
const audioFixture = execFileSync(process.platform==='win32'?'.venv/Scripts/ffmpeg.exe':'ffmpeg',[
    '-v','error','-f','lavfi','-i','sine=frequency=440:duration=2','-c:a','aac','-f','mp4','-movflags','frag_keyframe+empty_moov','pipe:1'
]);
const caps = {schema_version:1,generation_modes:['text_to_video','first_frame','first_last_frame','full_reference'],aspect_ratios:['16:9','9:16'],duration_resolution_map:[{durations:[4,6,12],resolutions:['720p','1080p']}],audio_policy:'disabled',reference_limits:{image:{enabled:true,min_count:0,max_count:5},video:{enabled:true,min_count:0,max_count:1,accepted_mime_types:['video/mp4']}}};
const models = ['text','image','video','tts'].map(type=>({id:`model-${type}`,name:`测试${type}模型`,type,is_default:true,capabilities:type==='video'?caps:{aspect_ratios:['1:1','16:9','9:16']}}));
const browser = await puppeteer.launch({executablePath:`${process.env.LOCALAPPDATA}/ms-playwright/chromium-1228/chrome-win64/chrome.exe`,headless:true,args:['--disable-gpu']});
try {
    for (const width of [1366,390]) {
        const page = await browser.newPage();
        const errors = [];
        const storage = new Map();
        const touchOnly = process.env.CANVAS_TOUCH_ONLY === '1';
        const canvasKey = 'infinite-canvas.app_state/infinite-canvas:canvas_store';
        if (touchOnly) {
            const nodes = ['touch-a','touch-b'].map((id,index)=>({id,type:'image',title:id,width:240,height:200,position:{x:80+index*350,y:500},metadata:{}}));
            storage.set(canvasKey,{revision:1,kind:'json',mime:'application/json',body:JSON.stringify(JSON.stringify({state:{projects:[{id:'touch-project',title:'触摸测试',createdAt:new Date().toISOString(),updatedAt:new Date().toISOString(),nodes,connections:[],chatSessions:[],activeChatId:null,backgroundMode:'lines',showImageInfo:false,viewport:{x:0,y:0,k:0.5}}],deletedProjects:[]},version:0}))});
        }
        const configKey='infinite-canvas.app_state/infinite-canvas:ai_config_store:test-user';
        const sourcesKey='infinite-canvas.app_state/infinite-canvas:prompt_source_store_v2:test-user';
        const customChannel={id:'custom-test',name:'自定义测试渠道',baseUrl:'https://provider.example.test/v1',apiKey:'test-only-key',apiFormat:'openai',models:[{name:'model-image',capability:'image',script:`window.__customCanvasCall = {model, prompt, apiKey}; return ['data:image/png;base64,${image.toString('base64')}'];`}]};
        storage.set(configKey,{revision:1,kind:'json',mime:'application/json',body:JSON.stringify(JSON.stringify({state:{config:{channels:[customChannel],imageModel:'cineforge::model-image',videoModel:'cineforge::model-video',textModel:'cineforge::model-text',audioModel:'cineforge::model-tts',videoSeconds:'6'},webdav:{url:'https://webdav.example.test',username:'test'}},version:0}))});
        storage.set(sourcesKey,{revision:1,kind:'json',mime:'application/json',body:JSON.stringify(JSON.stringify({state:{sources:[{id:'local-test',name:'本地测试模板',url:'/test-prompts.json',homepage:'',enabled:true,builtIn:false}],schedule:{intervalMinutes:0,lastFetchedAt:''}},version:0}))});
        const tasks = new Map();
        const generations = [];
        const evidenceRequests = [];
        const mediaRequests = [];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('console',msg=>{if(msg.type()==='error') console.log(`UI ${width}: ${msg.text()}`);});
        await page.setViewport({width,height:900,hasTouch:touchOnly});
        await page.evaluateOnNewDocument(()=>{localStorage.setItem('cineforge.access-token','canvas-ui-test');localStorage.setItem('cineforge-theme','dark');});
        await page.setRequestInterception(true);
        page.on('request',req=>{
            const url = new URL(req.url());
            const path = decodeURIComponent(url.pathname);
            if(path==='/canvas-video-fixture.mp4') return req.respond({status:200,contentType:'video/mp4',body:videoFixture});
            if(url.hostname==='raw.githubusercontent.com') return req.respond({status:200,headers:{'Access-Control-Allow-Origin':'*'},contentType:'application/json',body:'[]'});
            if (path==='/test-prompts.json') return req.respond({status:200,contentType:'application/json',body:JSON.stringify([{id:'local-prompt',title:'身份一致性测试模板',prompt:'保持人物五官和服装一致，仅改变背景',tags:['人物']}])});
            if (path.startsWith('/test-canvas')) return req.respond({status:200,contentType:'image/png',body:image});
            if(!path.startsWith('/api/')) return req.continue();
            const respond = (data,status=200,headers={})=>req.respond({status,headers,contentType:'application/json',body:status===204?'':JSON.stringify(data)});
            if(path.endsWith('/canvas/config')) return respond({user_id:'test-user',is_admin:true,models});
            if(path.endsWith('/canvas/video-evidence')) {
                evidenceRequests.push(JSON.parse(req.postData()));
                const frames = [0.25,0.75,1.25,1.75].map((at,i)=>({at,storageKey:`video-evidence:frame-${evidenceRequests.length}-${i}`}));
                const sheets = [{storageKey:`video-evidence:sheet-${evidenceRequests.length}`}];
                for(const f of [...frames,...sheets]) storage.set(`infinite-canvas.image_files/${f.storageKey}`,{revision:1,kind:'blob',mime:'image/webp',body:image});
                return respond({duration:2,start:0,end:2,has_audio:false,source_key:'video:probe',source_revision:1,frames,sheets});
            }
            if(path.endsWith('/canvas/media-process')) {
                const input=JSON.parse(req.postData()); mediaRequests.push(input);
                const storageKey=`${input.operation}:probe-${mediaRequests.length}`;
                const mimeType=input.operation==='extract_audio'?'audio/mp4':'video/mp4';
                storage.set(`infinite-canvas.media_files/${storageKey}`,{revision:1,kind:'blob',mime:mimeType,body:mimeType.startsWith('audio/')?audioFixture:videoFixture});
                return respond({storageKey,mimeType,bytes:videoFixture.length,duration:2,width:96,height:64,source_start:input.start || 0,source_end:input.end || 2});
            }
            if(path.includes('/canvas/storage/')) {
                const key = path.split('/canvas/storage/')[1];
                const record = storage.get(key);
                if(req.method()==='PUT') {
                    const revision=Number(url.searchParams.get('revision'));
                    if((record?.revision || 0)!==revision) return respond({detail:'revision conflict'},409);
                    storage.set(key,{revision:revision+1,kind:req.headers()['x-canvas-kind'],mime:req.headers()['content-type'],body:req.postData()});
                    return respond({revision:revision+1});
                }
                if(req.method()==='DELETE') {storage.delete(key);return respond(null,204);}
                if(!key.includes('/')) return respond([...storage.keys()].filter(k=>k.startsWith(key+'/')).map(k=>k.slice(key.length+1)));
                if(!record) return respond(null,204,{'X-Canvas-Revision':'0'});
                return req.respond({status:200,contentType:record.mime,headers:{'X-Canvas-Revision':String(record.revision),'X-Canvas-Kind':record.kind},body:record.kind==='blob'?(record.mime.startsWith('video/')?videoFixture:record.mime.startsWith('audio/')?audioFixture:image):record.body});
            }
            if(path.endsWith('/canvas/generate')) {
                const payload=JSON.parse(req.postData());generations.push(payload);
                const id=`canvas-task-${generations.length}`;
                const timeline={summary:'原片人物连续动作，未提供音频转写；保持场景与时间轴。',segments:[{start:0,end:2,description:'蓝色背景中的人物',action:'人物连续转身并举手，动作衔接不中断',camera:'固定中景',lighting:'柔和侧光',videoPrompt:'0–2秒人物连续转身并举手，固定中景，柔和侧光；保持身份与原片节奏。',frameTimes:[0.25,0.75,1.75]}]};
                const text=payload.prompt.includes('严格输出一个 JSON 对象')?JSON.stringify(timeline):'已参考当前人物图：保持人物身份，仅改变服装颜色。';
                tasks.set(id,{id,status:payload.expected_kind==='video'?'queued':'succeeded',result_payload:payload.expected_kind==='text'?{text}:{media_url:'/test-canvas-result.png'}});
                return respond({id},202);
            }
            if(path.includes('/tasks/canvas-task-')) {
                if(path.endsWith('/cancel')) {const task=tasks.get(path.split('/').at(-2));task.status='cancelled';return respond(task);}
                return respond(tasks.get(path.split('/').at(-1)));
            }
            if(path.endsWith('/auth/me')) return respond({user:{id:'test-user',role:'admin',display_name:'画布测试',background_blur:0},credit_balance:'1000'});
            if(path.endsWith('/notifications') || path.endsWith('/tasks')) return respond({items:[],next_before:null,unread_count:0});
            if(path.endsWith('/assets')) return respond([{id:'test-character',name:'测试角色',media_url:'/test-canvas-reference.png',generation_prompt:'保持角色面孔一致',description:'人物参考'}]);
            if(path.endsWith('/projects')) return respond([{id:'project-test',name:'测试项目'}]);
            return respond([]);
        });
        await page.goto('http://127.0.0.1:5173/canvas',{waitUntil:'domcontentloaded'});
        await page.waitForSelector('iframe');
        let frame = await (await page.$('iframe')).contentFrame();
        assert(frame,'Canvas iframe missing');
        await frame.waitForFunction(()=>document.body.textContent.includes('新画布'));
        async function clickText(text) {
            const buttons=await frame.$$('button');
            for (const button of buttons) {
                if ((await button.evaluate(el=>el.textContent.replace(/\s/g,'')))===text.replace(/\s/g,'')) {await button.click();return;}
            }
            throw new Error(`Button missing: ${text}`);
        }
        async function waitFocusedNode(id) {
            await frame.evaluate(async id=>{
                let previous='',stable=0;
                while(stable<10) {
                    await new Promise(resolve=>requestAnimationFrame(resolve));
                    const element=document.querySelector(`[data-node-id="${id}"]`);
                    const rect=element?.getBoundingClientRect();
                    const next=rect?`${rect.left.toFixed(1)},${rect.top.toFixed(1)},${rect.width.toFixed(1)}`:'';
                    stable=next && next===previous?stable+1:0;
                    previous=next;
                }
                const node=document.querySelector(`[data-node-id="${id}"]`).getBoundingClientRect();
                const surface=document.querySelector('.canvas-editor-layout > section').getBoundingClientRect();
                if(node.left<surface.left || node.right>surface.right || node.top<surface.top || node.bottom>surface.bottom) throw new Error('Focused node must fit inside the canvas: '+JSON.stringify({id,node:node.toJSON(),surface:surface.toJSON()}));
            },id).catch(async error=>{await page.screenshot({path:`.logs/infinite-canvas-focus-failed-${width}.png`});throw error;});
        }
        if (touchOnly) {
            await frame.waitForSelector('article h2');
            await frame.click('article h2');
            await frame.waitForSelector('[data-node-id="touch-a"]');
            // The side panel animates on entering a project; measure after the canvas origin settles.
            await frame.evaluate(async()=>{
                let previous=-1,stable=0;
                while(stable<8) {
                    await new Promise(resolve=>requestAnimationFrame(resolve));
                    const x=document.querySelector('.canvas-editor-layout > section').getBoundingClientRect().left;
                    stable=Math.abs(x-previous)<0.1?stable+1:0;
                    previous=x;
                }
            });
            const client = await page.createCDPSession();
            const offset = await page.$eval('iframe',el=>{const r=el.getBoundingClientRect();return {x:r.left,y:r.top};});
            async function bounds(selector) {
                return frame.$eval(selector,el=>{const r=el.getBoundingClientRect();return {x:r.left,y:r.top,w:r.width,h:r.height};});
            }
            const center = r=>({x:r.x+r.w/2+offset.x,y:r.y+r.h/2+offset.y});
            async function touch(type,points) {
                await client.send('Input.dispatchTouchEvent',{type,touchPoints:points.map((p,i)=>({...p,id:i+1,radiusX:2,radiusY:2,force:1}))});
            }
            async function drag(start,end,cancel=false) {
                await touch('touchStart',[start]);
                for(let i=1;i<=6;i++) {
                    await touch('touchMove',[{x:start.x+(end.x-start.x)*i/6,y:start.y+(end.y-start.y)*i/6}]);
                    await new Promise(resolve=>setTimeout(resolve,20));
                }
                await touch(cancel?'touchCancel':'touchEnd',[]);
            }
            let initial=await bounds('[data-node-id="touch-a"]');
            await drag(center(initial),{x:center(initial).x+20,y:center(initial).y+25});
            await frame.waitForFunction((before)=>{const r=document.querySelector('[data-node-id="touch-a"]').getBoundingClientRect();return Math.abs(r.left-before.x-20)<1 && Math.abs(r.top-before.y-25)<1;},{},initial);
            const source='[data-node-id="touch-a"] [data-connection-handle="source"]';
            let from=center(await bounds(source));
            let target=center(await bounds('[data-node-id="touch-b"]'));
            assert((await bounds(source)).w>=44,'Connection handle must stay finger-sized at half zoom');
            await drag(from,target);
            await frame.waitForSelector('[data-connection-id]');
            assert.equal(await frame.$$eval('[data-connection-id]',els=>els.length),1,'Touch connection should create exactly one edge');
            // Repeating the same connection must not duplicate it.
            await drag(center(await bounds(source)),target);
            assert.equal(await frame.$$eval('[data-connection-id]',els=>els.length),1);
            // A cancelled connection must not show the create-node menu or leave a gesture stuck.
            await drag(center(await bounds(source)),{x:120+offset.x,y:580+offset.y},true);
            assert.equal(await frame.$$eval('[data-connection-create-menu]',els=>els.length),0);
            initial=await bounds('[data-node-id="touch-a"]');
            await drag(center(initial),{x:center(initial).x+10,y:center(initial).y+10},true);
            initial=await bounds('[data-node-id="touch-a"]');
            await drag(center(initial),{x:center(initial).x-10,y:center(initial).y-10});
            await frame.waitForFunction(before=>Math.abs(document.querySelector('[data-node-id="touch-a"]').getBoundingClientRect().left-before.x+10)<1,{},initial);
            const beforeResize=await bounds('[data-node-id="touch-a"]');
            const corner=center(await bounds('[data-node-id="touch-a"] [data-resize-corner="bottom-right"]'));
            await drag(corner,{x:corner.x+10,y:corner.y+10});
            await frame.waitForFunction(before=>document.querySelector('[data-node-id="touch-a"]').getBoundingClientRect().width>before.w+9,{},beforeResize);
            // Background panning must move both nodes together, rather than scrolling the page.
            const beforePan=await bounds('[data-node-id="touch-a"]');
            const surface=await bounds('[data-canvas-gesture-background]');
            const panStart={x:surface.x+surface.w/2+offset.x,y:surface.y+150+offset.y};
            await drag(panStart,{x:panStart.x+10,y:panStart.y+15});
            await frame.waitForFunction(before=>{const r=document.querySelector('[data-node-id="touch-a"]').getBoundingClientRect();return Math.abs(r.left-before.x-10)<1 && Math.abs(r.top-before.y-15)<1;},{},beforePan);
            assert.equal(await page.evaluate(()=>scrollY),0);
            // Mouse drag regression in the same production graph.
            const beforeMouse=await bounds('[data-node-id="touch-a"]');
            const mouseStart=center(beforeMouse);
            await page.mouse.move(mouseStart.x,mouseStart.y);
            await page.mouse.down();
            await page.mouse.move(mouseStart.x+12,mouseStart.y+8,{steps:5});
            await page.mouse.up();
            await frame.waitForFunction(before=>Math.abs(document.querySelector('[data-node-id="touch-a"]').getBoundingClientRect().left-before.x-12)<1,{},beforeMouse);
            // The title remains a drag surface for nodes with interactive content/players.
            const beforeTitle=await bounds('[data-node-id="touch-a"]');
            const titleStart=center(await bounds('[data-node-id="touch-a"] [data-node-drag-title]'));
            await drag(titleStart,{x:titleStart.x+8,y:titleStart.y+6});
            await frame.waitForFunction(before=>Math.abs(document.querySelector('[data-node-id="touch-a"]').getBoundingClientRect().left-before.x-8)<1,{},beforeTitle);
            // Save/reload proves gestures update business state, not only a temporary DOM preview.
            const final=await bounds('[data-node-id="touch-a"]');
            for(let i=0;i<30;i++) {
                const saved=JSON.parse(JSON.parse(storage.get(canvasKey).body)).state.projects[0];
                if(saved.connections.length===1 && Math.abs(saved.nodes[0].position.x*saved.viewport.k+saved.viewport.x+surface.x-final.x)<2) break;
                await new Promise(resolve=>setTimeout(resolve,100));
            }
            const saved=JSON.parse(JSON.parse(storage.get(canvasKey).body)).state.projects[0];
            assert.equal(saved.connections.length,1);
            assert.equal(saved.connections[0].fromNodeId,'touch-a');
            assert.equal(saved.connections[0].toNodeId,'touch-b');
            assert(saved.nodes[0].width>240);
            assert(Math.abs(saved.nodes[0].position.x*saved.viewport.k+saved.viewport.x+surface.x-final.x)<2,'Final position must be persisted');
            await page.screenshot({path:`.logs/infinite-canvas-touch-${width}.png`,fullPage:true});
            await page.reload({waitUntil:'domcontentloaded'});
            await page.waitForSelector('iframe');
            frame=await (await page.$('iframe')).contentFrame();
            await frame.waitForSelector('article h2');
            await frame.click('article h2');
            await frame.waitForSelector('[data-connection-id]');
            assert.deepEqual(errors,[]);
            assert.equal(generations.length,0,'Gesture verification must not call a model');
            console.log(`${width}px: real touch drag/connect/duplicate prevention/resize/cancel recovery/pan, mouse drag and persisted reload passed`);
            await page.close();
            continue;
        }
        await clickText('模型设置');
        await frame.waitForFunction(()=>document.body.textContent.includes('自定义测试渠道'));
        assert(await frame.evaluate(()=>document.body.textContent.includes('系统管理')),'Managed channel must be protected');
        await page.screenshot({path:`.logs/infinite-canvas-config-${width}.png`,fullPage:true});
        const originalDatabases = await frame.evaluate(async () => {
            const names = (await indexedDB.databases()).map(db => db.name);
            Object.defineProperty(navigator, 'storage', {configurable:true, value:undefined});
            return names;
        });
        await frame.evaluate(() => [...document.querySelectorAll('[role="tab"]')].find(el => el.textContent === '本地存储').click());
        await frame.waitForFunction(() => document.body.textContent.includes('浏览器未提供'));
        assert(await frame.evaluate(() => !document.body.textContent.includes('读取本地存储失败') && document.body.textContent.includes('保存在服务端') && !document.body.textContent.includes('NaN%')));
        assert.deepEqual(await frame.evaluate(async () => (await indexedDB.databases()).map(db => db.name)), originalDatabases, 'Reading statistics must not create a database');
        // Seed actual local content and simulate a browser that refuses storage estimation.
        await frame.evaluate(async () => {
            Object.defineProperty(navigator, 'storage', {configurable:true, value:{estimate:async () => {throw new Error('estimate denied');}}});
            await new Promise((resolve, reject) => {
                const request = indexedDB.open('infinite-canvas');
                request.onupgradeneeded = () => request.result.createObjectStore('probe-local-cache');
                request.onerror = () => reject(request.error);
                request.onsuccess = () => {
                    const db=request.result;
                    const tx=db.transaction('probe-local-cache','readwrite');
                    tx.objectStore('probe-local-cache').put(new Blob(['x'.repeat(2048)]), 'fixture');
                    tx.oncomplete=()=>{db.close();resolve();};
                    tx.onerror=()=>{db.close();reject(tx.error);};
                };
            });
        });
        await clickText('刷新统计');
        await frame.waitForFunction(() => document.body.textContent.includes('2.0 KB') && document.body.textContent.includes('probe-local-cache'));
        assert(await frame.evaluate(() => !document.body.textContent.includes('读取本地存储失败') && !document.body.textContent.includes('NaN%')));
        await page.screenshot({path:`.logs/infinite-canvas-storage-${width}.png`,fullPage:true});
        // A supported estimate restores the quota display and progress on refresh.
        await frame.evaluate(() => Object.defineProperty(navigator, 'storage', {configurable:true, value:{estimate:async () => ({usage:1024,quota:4096})}}));
        await clickText('刷新统计');
        await frame.waitForFunction(() => document.body.textContent.includes('25.00%'));
        console.log(`${width}px: missing/rejected storage estimates preserve real IndexedDB statistics; quota recovery and no database creation passed`);
        if (process.env.CANVAS_STORAGE_ONLY === '1') {
            assert.deepEqual(errors, []);
            await page.close();
            continue;
        }
        await clickText('模板中心');
        await frame.waitForFunction(()=>document.body.textContent.includes('身份一致性测试模板'));
        await clickText('画布库');
        await clickText('新画布');
        await frame.waitForFunction(()=>document.body.textContent.includes('画布助手'));
        await frame.click('button[aria-label="连接 Codex"]');
        await frame.waitForFunction(()=>document.body.textContent.includes('启动并连接本地 Codex'));
        await frame.waitForFunction(()=>Math.abs(document.querySelector('.canvas-codex-panel').getBoundingClientRect().width-Math.min(innerWidth,441))<2);
        const panelBounds=await frame.$eval('.canvas-codex-panel',el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,width:innerWidth};});
        assert(panelBounds.left>=-1 && panelBounds.right<=panelBounds.width+1,'Codex panel must fit viewport');
        await page.screenshot({path:`.logs/infinite-canvas-codex-${width}.png`,fullPage:true});
        await frame.evaluate(()=>document.querySelector('button[aria-label="连接 Codex"]').click());
        await frame.waitForFunction(()=>getComputedStyle(document.querySelector('.canvas-codex-panel')).pointerEvents==='none');
        await clickText('资产库');
        await frame.waitForFunction(()=>document.body.textContent.includes('测试角色'));
        await clickText('测试角色');
        await frame.waitForFunction(()=>document.querySelector('img[src^="blob:"]'));
        await page.waitForFunction(()=>document.body.textContent.includes('已连接'));
        await frame.waitForFunction(()=>!document.querySelector('.ant-modal-mask'));
        await frame.click('img[src^="blob:"]');
        await page.screenshot({path:`.logs/infinite-canvas-ready-${width}.png`,fullPage:true});
        await clickText('画布助手');
        await frame.waitForSelector('textarea[placeholder^="描述想法"]');
        await frame.type('textarea[placeholder^="描述想法"]','帮我设计蓝色服装的人物提示词');
        await clickText('发送');
        try { await frame.waitForFunction(()=>document.body.textContent.includes('已参考当前人物图')); }
        catch(error) { console.log('generations:',JSON.stringify(generations));await page.screenshot({path:`.logs/infinite-canvas-failed-${width}.png`,fullPage:true});if(!frame.detached) console.log(await frame.evaluate(()=>document.body.textContent.slice(-1800)));throw error; }
        await clickText('加入文本节点');
        await frame.waitForFunction(()=>document.body.textContent.includes('已加入文本节点'));
        await frame.click('.ant-drawer-close');
        await frame.waitForFunction(()=>!document.querySelector('.ant-drawer-mask'));
        await page.screenshot({path:`.logs/infinite-canvas-${width}.png`,fullPage:true});
        await frame.waitForFunction(()=>!document.querySelector('[role="status"]'));
        // Wait for the debounced server save, then inspect the exact persisted state.
        await page.waitForFunction(()=>document.body.textContent.includes('已连接'));
        let saved;
        for(let i=0;i<30;i++) {
            const record=storage.get('infinite-canvas.app_state/infinite-canvas:canvas_store');
            if(record) {saved=JSON.parse(JSON.parse(record.body)).state.projects[0];if(saved?.nodes.length>=2) break;}
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert.equal(saved.nodes.length,2,'Imported asset and assistant node must be saved');
        assert(saved.nodes.some(node=>node.title==='测试角色'));
        assert.equal(generations[0].model_id,'model-text');
        assert(generations[0].reference_keys.length > 0,'Selected image must be sent to the assistant');
        await page.reload({waitUntil:'domcontentloaded'});
        await page.waitForSelector('iframe');
        frame=await (await page.$('iframe')).contentFrame();
        await frame.waitForFunction(()=>document.body.textContent.includes('新画布'));
        const restoredConfig=JSON.parse(JSON.parse(storage.get(configKey).body)).state;
        assert.deepEqual(restoredConfig.config.channels.find(c=>c.id==='custom-test'),customChannel,'Custom scripts and channel credentials must survive bootstrap');
        assert.equal(restoredConfig.webdav.url,'https://webdav.example.test');
        // Open saved project through its visible card.
        const card=await frame.$(`[data-project-id="${saved.id}"]`);
        if(card) await card.click();
        else await frame.evaluate(id=>{location.hash=`#/canvas/${id}`;},saved.id);
        await frame.waitForFunction(()=>document.querySelector('img[src^="blob:"]'));
        const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
        assert.equal(overflow,false,'Host must not overflow horizontally');
        await frame.click('.canvas-creation-toolbar button[aria-label="图片"]');
        await frame.waitForSelector('[contenteditable="true"]');
        async function checkSettingsMenu(label, option) {
            await frame.evaluate(label => {
                const field = [...document.querySelectorAll('.canvas-settings-surface label')].find(el => el.textContent.startsWith(label));
                if (!field) throw new Error(`Settings field missing: ${label}`);
                const select = field.querySelector('.ant-select-selector') || field.querySelector('.ant-select');
                if (!select) throw new Error(field.outerHTML);
                select.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
            }, label);
            await frame.waitForSelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');
            const boundsHandle = await frame.waitForFunction(option => {
                const item=[...document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')].find(el=>el.textContent.trim()===option && el.getBoundingClientRect().width>0);
                if (!item) return false;
                const rect=item.getBoundingClientRect();
                const hit=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
                if(!item.contains(hit)) return false;
                const dropdown=item.closest('.ant-select-dropdown');
                const bounds={nested:!!dropdown.closest('.canvas-settings-surface'),clipped:!!dropdown.closest('.canvas-settings-scroll'),hittable:true,rect:rect.toJSON(),hit:hit?.outerHTML.slice(0,400)};
                item.click();
                return bounds;
            }, {timeout:3000}, option).catch(async error => {
                await page.screenshot({path:`.logs/infinite-canvas-settings-failed-${width}.png`});
                console.log(await frame.evaluate(() => {const d=document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)');const r=d.getBoundingClientRect(); return {html:d.outerHTML.slice(0,1200),rect:r.toJSON(),hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.outerHTML.slice(0,500)};}));
                throw error;
            });
            const bounds = await boundsHandle.jsonValue();
            await page.screenshot({path:`.logs/infinite-canvas-settings-${label}-${width}.png`});
            assert(bounds.nested && !bounds.clipped && bounds.hittable, JSON.stringify(bounds));
            await frame.waitForFunction(() => !document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden)'));
            assert(await frame.$('.canvas-settings-surface'), 'Choosing an option must keep settings open');
            await frame.waitForFunction((label, option) => [...document.querySelectorAll('.canvas-settings-surface label')].find(el => el.textContent.startsWith(label))?.querySelector('.ant-select')?.textContent.includes(option), {}, label, option);
        }
        async function openSettings() {
            await frame.evaluate(() => [...document.querySelector('[contenteditable="true"]').closest('[data-canvas-no-zoom]').querySelectorAll('button')].find(el => el.querySelector('svg.lucide-settings-2')).click());
            await frame.waitForSelector('.canvas-settings-surface');
        }
        await openSettings();
        await checkSettingsMenu('图片数量','3 张');
        await checkSettingsMenu('图片数量','1 张');
        if (width === 390) {
            await page.setViewport({width:320,height:900});
            await frame.waitForFunction(() => {const r=document.querySelector('.canvas-settings-surface').getBoundingClientRect(); return r.left >= 12 && r.right <= innerWidth - 11;});
            await checkSettingsMenu('画幅','1:1');
            await page.setViewport({width,height:900});
        }
        await frame.evaluate(() => document.body.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true})));
        await frame.waitForFunction(() => !document.querySelector('.canvas-settings-surface'));
        console.log(`${width}px: settings menus are above the panel, selectable and contained outside its scroll area`);
        await frame.type('[contenteditable="true"]','生成蓝色花朵');
        await frame.click('button[aria-label="生成"]');
        await frame.waitForFunction(()=>!document.querySelector('button[aria-label="停止生成"]'));
        assert(generations.some(item=>item.expected_kind==='image' && item.model_id==='model-image' && item.prompt==='生成蓝色花朵'));
        await frame.click('.canvas-creation-toolbar button[aria-label="视频"]');
        await frame.waitForSelector('[contenteditable="true"]');
        await openSettings();
        await checkSettingsMenu('视频时长','6 秒');
        await frame.evaluate(() => document.body.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true})));
        await frame.waitForFunction(() => !document.querySelector('.canvas-settings-surface'));
        await frame.type('[contenteditable="true"]','摄影机缓慢推近花朵');
        await frame.click('button[aria-label="生成"]');
        await frame.waitForSelector('button[aria-label="停止生成"]');
        for(let i=0;i<50 && !generations.some(item=>item.expected_kind==='video');i++) await new Promise(resolve=>setTimeout(resolve,100));
        const video=generations.find(item=>item.expected_kind==='video');
        assert(video && video.model_id==='model-video' && video.aspect_ratio==='16:9' && video.audio_enabled===false && [4,6,12].includes(video.duration_seconds),JSON.stringify(video));
        await frame.click('button[aria-label="停止生成"]');
        await frame.waitForSelector('.ant-modal-confirm');
        await frame.$eval('.ant-modal-confirm .ant-btn-dangerous',button=>button.click());
        await frame.waitForFunction(()=>!document.querySelector('.ant-modal-mask'));
        await frame.waitForFunction(()=>!document.querySelector('button[aria-label="停止生成"]'));
        await frame.click('.canvas-creation-toolbar button[aria-label="图片"]');
        await frame.waitForSelector('[contenteditable="true"]');
        const pickers=await frame.$$('button.canvas-composer-model-picker');
        await pickers.at(-1).click();
        await frame.waitForSelector('[role="option"]');
        await frame.evaluate(()=>[...document.querySelectorAll('[role="option"]')].find(el=>el.textContent.includes('自定义测试渠道')).click());
        await frame.type('[contenteditable="true"]','仅走自定义渠道测试');
        const beforeCustom=generations.length;
        await frame.click('button[aria-label="生成"]');
        await frame.waitForFunction(()=>window.__customCanvasCall?.prompt==='仅走自定义渠道测试');
        assert.equal(generations.length,beforeCustom,'Custom channel must not use managed gateway, even with colliding model names');
        assert.equal(await frame.evaluate(()=>window.__customCanvasCall.model),'model-image');
        await frame.waitForFunction(()=>!document.querySelector('button[aria-label="停止生成"]'));
        if(width===390) {
            await page.setViewport({width:900,height:390});
            await frame.waitForFunction(()=>document.querySelector('.canvas-creation-toolbar'));
            await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth+1);
            await page.setViewport({width:390,height:900});
            await frame.waitForFunction(()=>{
                const rect=document.querySelector('.canvas-creation-toolbar')?.getBoundingClientRect();
                return rect && rect.left>=0 && rect.right<=innerWidth+1;
            });
        }
        assert.deepEqual(errors,[]);
        await page.goto('http://127.0.0.1:5173/canvas?mode=new#agentUrl=http%3A%2F%2F127.0.0.1%3A5173%2Fapi%2Fv1%2Fcanvas%2Fagent-test&agentToken=fragment-test',{waitUntil:'domcontentloaded'});
        await page.waitForSelector('iframe');
        frame=await (await page.$('iframe')).contentFrame();
        await frame.waitForSelector('.canvas-editor-layout');
        await frame.waitForFunction(()=>localStorage.getItem('canvas-agent-token:test-user')==='fragment-test');
        assert(!page.url().includes('agentToken'),'Host must remove connection credentials from address');
        assert(!frame.url().includes('agentToken'),'Canvas must remove handoff fragment after receipt');
        assert.equal(await frame.evaluate(()=>new URLSearchParams(location.hash.split('?')[1]).get('mode')),'new','Handoff must preserve create-canvas mode');
        if (await frame.evaluate(()=>getComputedStyle(document.querySelector('.canvas-codex-panel')).pointerEvents!=='none')) {
            await frame.evaluate(()=>document.querySelector('button[aria-label="连接 Codex"]').click());
            await frame.waitForFunction(()=>getComputedStyle(document.querySelector('.canvas-codex-panel')).pointerEvents==='none');
        }
        for(let i=0;i<30;i++) {
            const state=JSON.parse(JSON.parse(storage.get('infinite-canvas.app_state/infinite-canvas:canvas_store').body)).state;
            if(state.projects[0].id!==saved.id) break;
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        storage.set('infinite-canvas.media_files/video:probe',{revision:1,kind:'blob',mime:'video/mp4',body:videoFixture});
        storage.set('infinite-canvas.image_files/image:replica-person',{revision:1,kind:'blob',mime:'image/png',body:image});
        await page.keyboard.press('Escape');
        const currentSaved=JSON.parse(JSON.parse(storage.get('infinite-canvas.app_state/infinite-canvas:canvas_store').body)).state.projects[0];
        await frame.evaluate(({id})=>{
            const surface=document.querySelector('.canvas-editor-layout > section').getBoundingClientRect();
            const matrix=new DOMMatrix(getComputedStyle(document.querySelector('[data-canvas-gesture-background]').parentElement.querySelector('.origin-top-left')).transform);
            const viewport={x:matrix.e,y:matrix.f,k:matrix.a};
            window.dispatchEvent(new CustomEvent('cineforge:add-node',{detail:{projectId:id,node:{id:'replica-person',type:'image',title:'用户替换人物图',width:80,height:80,
                position:{x:(surface.width/2-viewport.x)/viewport.k-140,y:(surface.height/2-viewport.y)/viewport.k},
                metadata:{content:'/test-canvas-reference.png',storageKey:'image:replica-person',mimeType:'image/png',naturalWidth:1,naturalHeight:1,status:'success'}}}}));
            window.dispatchEvent(new CustomEvent('cineforge:add-node',{detail:{projectId:id,node:{id:'video-analysis-probe',type:'video',title:'视频分析测试',width:200,height:112,
                position:{x:(surface.width/2-viewport.x)/viewport.k,y:(surface.height/2-viewport.y)/viewport.k},
                metadata:{content:'/canvas-video-fixture.mp4',storageKey:'video:probe',mimeType:'video/mp4',durationMs:2000,status:'success'}}}}));
        },{id:currentSaved.id,viewport:currentSaved.viewport});
        await frame.waitForSelector('[data-node-id="video-analysis-probe"]');
        await frame.click('[data-node-id="replica-person"]',{offset:{x:40,y:40}});
        const connectionPoints=await frame.evaluate(()=>{
            const handle=document.querySelector('[data-node-id="replica-person"] [data-connection-handle="source"]').getBoundingClientRect();
            const video=document.querySelector('[data-node-id="video-analysis-probe"]').getBoundingClientRect();
            return {start:{x:handle.left+handle.width/2,y:handle.top+handle.height/2},end:{x:video.left+video.width/2,y:video.top+video.height/2}};
        });
        const iframeOffset=await page.$eval('iframe',el=>{const r=el.getBoundingClientRect();return {x:r.left,y:r.top};});
        await page.mouse.move(connectionPoints.start.x+iframeOffset.x,connectionPoints.start.y+iframeOffset.y);
        await page.mouse.down();
        await page.mouse.move(connectionPoints.end.x+iframeOffset.x,connectionPoints.end.y+iframeOffset.y,{steps:8});
        await page.mouse.up();
        await frame.waitForSelector('[data-connection-id]');
        await frame.click('[data-node-id="video-analysis-probe"]',{offset:{x:12,y:12}});
        const beforeAnalysis=generations.length;
        const beforeEvidence=evidenceRequests.length;
        await frame.waitForSelector('.canvas-node-hover-toolbar button[aria-label="视频解析：关键帧与时间轴"]');
        const analysisButton=await frame.$('.canvas-node-hover-toolbar button[aria-label="视频解析：关键帧与时间轴"]');
        await analysisButton.evaluate(el=>el.scrollIntoView({block:'nearest',inline:'nearest'}));
        await analysisButton.click();
        await frame.waitForFunction(()=>document.body.textContent.includes('视频分析测试 · 视频分析'));
        for(let i=0;i<80 && generations.length===beforeAnalysis;i++) await new Promise(resolve=>setTimeout(resolve,100));
        assert(evidenceRequests.length>0,'Video analysis must actually extract frames');
        const analysis=generations.at(-1);
        assert(analysis.expected_kind==='text' && analysis.reference_keys.length>=2,'Text model must receive video evidence and the connected replacement character');
        assert(analysis.prompt.includes('不是原片分屏') && analysis.prompt.includes('可复刻'),'Analysis must carry timestamp interpretation and replication instructions');
        await frame.waitForFunction(()=>!document.querySelector('button[aria-label="停止生成"]'));
        let videoProject,output;
        for(let i=0;i<60;i++) {
            videoProject=JSON.parse(JSON.parse(storage.get('infinite-canvas.app_state/infinite-canvas:canvas_store').body)).state.projects[0];
            output=videoProject.nodes.find(n=>n.type==='text' && n.metadata?.videoAnalysis?.timeline);
            if(output) break;
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        assert(output,'Parsed timeline must be persisted on the output node');
        assert.equal(evidenceRequests.length-beforeEvidence,1,'Combined analysis must extract once and reuse the same evidence');
        assert.equal(output.metadata.videoAnalysis.evidence.frames.length,4);
        assert(output.metadata.videoAnalysis.referenceNodeIds.includes('replica-person'),'Connected identity image must remain linked to the parsed result');
        const group=videoProject.nodes.find(n=>n.id===output.metadata.videoAnalysis.groupNodeId);
        assert(group?.type==='group');
        assert.equal(videoProject.nodes.filter(n=>n.metadata?.groupId===group.id).length,4);
        assert(videoProject.nodes.some(n=>n.id==='video-analysis-probe' && n.metadata.content),'Original video must remain available');
        // Focus the timeline through the existing node list; mobile exposes it via the side-panel toggle.
        if(await frame.$('button[aria-label="展开面板"]')) await frame.click('button[aria-label="展开面板"]');
        await frame.waitForFunction(title=>[...document.querySelectorAll('button')].some(b=>b.title==='定位到节点' && b.textContent.includes(title)),{},output.title);
        await frame.evaluate(title=>{
            const focus=[...document.querySelectorAll('button')].find(b=>b.title==='定位到节点' && b.textContent.includes(title));
            if(!focus) throw new Error('Timeline list entry missing');
            focus.click();
        },output.title);
        if(width<640) await frame.click('button[aria-label="收起面板"]');
        await waitFocusedNode(output.id);
        await frame.waitForSelector('[data-video-analysis-timeline]');
        await frame.waitForFunction(()=>[...document.querySelectorAll('[data-video-analysis-timeline] img')].every(img=>img.complete && img.naturalWidth>0));
        await page.screenshot({path:`.logs/infinite-canvas-video-timeline-${width}.png`,fullPage:true});
        const beforeAssistant=generations.length;
        await clickText('画布助手');
        await frame.waitForSelector('textarea[placeholder^="描述想法"]');
        await frame.type('textarea[placeholder^="描述想法"]','基于当前时间轴和人物参考继续设计复刻');
        await clickText('发送');
        for(let i=0;i<60 && generations.length===beforeAssistant;i++) await new Promise(resolve=>setTimeout(resolve,100));
        const timelineAssistant=generations.at(-1);
        assert.equal(timelineAssistant.expected_kind,'text');
        assert.equal(timelineAssistant.reference_keys.length,4,'Assistant must receive timeline keyframes plus identity, not text alone');
        assert(timelineAssistant.reference_keys.every(ref=>storage.has(`${ref.namespace}/${ref.key}`)),'Assistant references must be real stored images');
        assert(timelineAssistant.prompt.includes('用户替换人物图') && timelineAssistant.prompt.includes('原片 0.25 秒画面证据'));
        assert(timelineAssistant.prompt.includes('0–2秒') && timelineAssistant.prompt.includes('人物连续转身并举手'));
        assert.equal(evidenceRequests.length-beforeEvidence,1,'Continuing from a saved timeline must not re-extract the video');
        await frame.waitForFunction(()=>!document.querySelector('[role="status"]'));
        await frame.click('.ant-drawer-close');
        await frame.waitForFunction(()=>!document.querySelector('.ant-drawer-mask'));
        await frame.evaluate(()=>document.querySelector('[data-video-analysis-timeline] article button').click());
        await frame.waitForFunction(()=>document.body.textContent.includes('视频复刻'));
        for(let i=0;i<60;i++) {
            videoProject=JSON.parse(JSON.parse(storage.get('infinite-canvas.app_state/infinite-canvas:canvas_store').body)).state.projects[0];
            if(videoProject.nodes.some(n=>n.title==='0–2s · 视频复刻')) break;
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        const replica=videoProject.nodes.find(n=>n.title==='0–2s · 视频复刻');
        assert(replica,'Timeline action must create a real generation config');
        assert.equal(replica.metadata.seconds,'4','Duration must respect the configured model');
        assert.equal(replica.metadata.videoMode,'reference');
        const linked=videoProject.connections.filter(c=>c.toNodeId===replica.id).map(c=>videoProject.nodes.find(n=>n.id===c.fromNodeId));
        assert.equal(linked.filter(n=>n.type==='image').length,4,'Replica must use segment images plus the replacement character');
        assert(linked.some(n=>n.id==='replica-person'));
        assert(linked.some(n=>n.type==='text' && n.metadata.content.includes('0–2秒')),'Replica must keep its segment timeline');
        assert(linked.some(n=>n.type==='video' && n.metadata.mediaSource?.operation==='trim_video'),'Video-capable models must receive the actual source clip');
        assert(mediaRequests.some(r=>r.operation==='trim_video' && r.start===0 && r.end===2));
        const beforeReplica=generations.length;
        if(await frame.$('button[aria-label="展开面板"]')) await frame.click('button[aria-label="展开面板"]');
        await frame.waitForFunction(title=>[...document.querySelectorAll('button')].some(b=>b.title==='定位到节点' && b.textContent.includes(title)),{},replica.title);
        await frame.evaluate(title=>[...document.querySelectorAll('button')].find(b=>b.title==='定位到节点' && b.textContent.includes(title)).click(),replica.title);
        if(width<640) await frame.click('button[aria-label="收起面板"]');
        await frame.waitForSelector(`[data-node-id="${replica.id}"]`);
        await waitFocusedNode(replica.id);
        await frame.evaluate(id=>{
            const button=[...document.querySelector(`[data-node-id="${id}"]`).querySelectorAll('button')].find(b=>b.textContent.includes('开始生成'));
            if(!button) throw new Error('Replica generation button missing');
            button.click();
        },replica.id);
        for(let i=0;i<60 && generations.length===beforeReplica;i++) await new Promise(resolve=>setTimeout(resolve,100));
        const replicaRequest=generations.at(-1);
        assert.equal(replicaRequest.expected_kind,'video');
        assert.equal(replicaRequest.reference_keys.length,5,'Actual video API call must include source video, segment frames and identity image');
        assert(replicaRequest.reference_keys.some(ref=>ref.namespace==='infinite-canvas.media_files' && ref.key.startsWith('trim_video:')));
        assert(replicaRequest.reference_keys.some(ref=>ref.key==='image:replica-person'));
        assert(replicaRequest.prompt.includes('0–2秒') && replicaRequest.prompt.includes('替换人物身份'));
        assert.equal(replicaRequest.duration_seconds,4);
        await page.screenshot({path:`.logs/infinite-canvas-video-analysis-${width}.png`,fullPage:true});
        const focusSource=async()=>{
            if(await frame.$('button[aria-label="展开面板"]')) await frame.click('button[aria-label="展开面板"]');
            await frame.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.title==='定位到节点' && b.textContent.includes('视频分析测试')));
            await frame.evaluate(()=>[...document.querySelectorAll('button')].find(b=>b.title==='定位到节点' && b.textContent.includes('视频分析测试') && !b.textContent.includes('·')).click());
            if(width<640) await frame.click('button[aria-label="收起面板"]');
            await waitFocusedNode('video-analysis-probe');
            await frame.click('[data-node-id="video-analysis-probe"]',{offset:{x:12,y:12}});
        };
        const toolbarClick=async label=>{
            const selector=`.canvas-node-hover-toolbar button[aria-label="${label}"]`;
            await frame.waitForSelector(selector);
            await frame.$eval(selector,el=>{el.scrollIntoView({block:'nearest',inline:'nearest'});el.click();});
        };
        await focusSource();
        await toolbarClick('提取原视频音轨，原片保留');
        await frame.waitForFunction(()=>document.body.textContent.includes('音轨已提取到画布'));
        for(let i=0;i<60;i++) {
            videoProject=JSON.parse(JSON.parse(storage.get(canvasKey).body)).state.projects[0];
            if(videoProject.nodes.some(n=>n.metadata?.mediaSource?.operation==='extract_audio' && n.metadata.status==='success')) break;
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        const audio=videoProject.nodes.find(n=>n.metadata?.mediaSource?.operation==='extract_audio');
        assert(audio?.metadata.content && audio.type==='audio');
        await focusSource();
        await toolbarClick('选择音轨封装到视频，生成新文件');
        await frame.waitForSelector('select[aria-label="选择画布音轨"]');
        await frame.waitForFunction(()=>{
            const modal=document.querySelector('.ant-modal');
            return modal && modal.getAnimations({subtree:true}).every(animation=>animation.playState!=='running');
        });
        await frame.select('select[aria-label="选择画布音轨"]',audio.id);
        await frame.$eval('input[aria-label="视频播放起点（秒）"]',el=>{
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,'0.3');
            el.dispatchEvent(new Event('input',{bubbles:true}));
        });
        assert(await frame.$eval('input[aria-label="视频播放起点（秒）"]',el=>{
            const r=el.getBoundingClientRect();
            return document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)===el;
        }),'Audio alignment input must be above canvas tools and fully clickable');
        await page.screenshot({path:`.logs/infinite-canvas-audio-mux-${width}.png`,fullPage:true});
        await frame.evaluate(()=>[...document.querySelectorAll('.ant-modal-footer button')].find(b=>b.textContent.includes('生成新视频')).click());
        await frame.waitForFunction(()=>document.body.textContent.includes('已创建带音轨的新视频'));
        assert(mediaRequests.some(r=>r.operation==='mux_audio' && r.audio_key===audio.metadata.storageKey && r.offset===0.3));
        assert.deepEqual(errors,[]);
        console.log(`${width}px: node-toolbar combined video parsing, one extraction, grouped timestamp frames, saved timeline and replica config with actual pictures/model duration passed`);
        console.log(`${width}px: canvas, Codex panel, custom channels/scripts, prompt sources, assets, assistant context, save/reload and generation parameters passed (${storage.size} records)`);
        await page.close();
    }
} finally { await browser.close(); }
