import assert from 'node:assert/strict';
import puppeteer from '../services/video-replica/node_modules/puppeteer-core/lib/puppeteer/puppeteer-core.js';

// Test the production Vue + React module graph, without modifying user data or calling a model.
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8WQAAAAASUVORK5CYII=', 'base64');
const caps = {schema_version:1,generation_modes:['text_to_video','first_frame','first_last_frame','full_reference'],aspect_ratios:['16:9','9:16'],duration_resolution_map:[{durations:[4,6,12],resolutions:['720p','1080p']}],audio_policy:'disabled',reference_limits:{image:{enabled:true,min_count:0,max_count:5}}};
const models = ['text','image','video','tts'].map(type=>({id:`model-${type}`,name:`测试${type}模型`,type,is_default:true,capabilities:type==='video'?caps:{aspect_ratios:['1:1','16:9','9:16']}}));
const browser = await puppeteer.launch({executablePath:`${process.env.LOCALAPPDATA}/ms-playwright/chromium-1228/chrome-win64/chrome.exe`,headless:true});
try {
    for (const width of [1366,390]) {
        const page = await browser.newPage();
        const errors = [];
        const storage = new Map();
        const configKey='infinite-canvas.app_state/infinite-canvas:ai_config_store:test-user';
        const sourcesKey='infinite-canvas.app_state/infinite-canvas:prompt_source_store_v2:test-user';
        const customChannel={id:'custom-test',name:'自定义测试渠道',baseUrl:'https://provider.example.test/v1',apiKey:'test-only-key',apiFormat:'openai',models:[{name:'model-image',capability:'image',script:`window.__customCanvasCall = {model, prompt, apiKey}; return ['data:image/png;base64,${image.toString('base64')}'];`}]};
        storage.set(configKey,{revision:1,kind:'json',mime:'application/json',body:JSON.stringify(JSON.stringify({state:{config:{channels:[customChannel],imageModel:'cineforge::model-image',videoModel:'cineforge::model-video',textModel:'cineforge::model-text',audioModel:'cineforge::model-tts',videoSeconds:'6'},webdav:{url:'https://webdav.example.test',username:'test'}},version:0}))});
        storage.set(sourcesKey,{revision:1,kind:'json',mime:'application/json',body:JSON.stringify(JSON.stringify({state:{sources:[{id:'local-test',name:'本地测试模板',url:'/test-prompts.json',homepage:'',enabled:true,builtIn:false}],schedule:{intervalMinutes:0,lastFetchedAt:''}},version:0}))});
        const tasks = new Map();
        const generations = [];
        page.on('pageerror',error=>errors.push(error.message));
        page.on('console',msg=>{if(msg.type()==='error') console.log(`UI ${width}: ${msg.text()}`);});
        await page.setViewport({width,height:900});
        await page.evaluateOnNewDocument(()=>{localStorage.setItem('cineforge.access-token','canvas-ui-test');localStorage.setItem('cineforge-theme','dark');});
        await page.setRequestInterception(true);
        page.on('request',req=>{
            const url = new URL(req.url());
            const path = decodeURIComponent(url.pathname);
            if(url.hostname==='raw.githubusercontent.com') return req.respond({status:200,headers:{'Access-Control-Allow-Origin':'*'},contentType:'application/json',body:'[]'});
            if (path==='/test-prompts.json') return req.respond({status:200,contentType:'application/json',body:JSON.stringify([{id:'local-prompt',title:'身份一致性测试模板',prompt:'保持人物五官和服装一致，仅改变背景',tags:['人物']}])});
            if (path.startsWith('/test-canvas')) return req.respond({status:200,contentType:'image/png',body:image});
            if(!path.startsWith('/api/')) return req.continue();
            const respond = (data,status=200,headers={})=>req.respond({status,headers,contentType:'application/json',body:status===204?'':JSON.stringify(data)});
            if(path.endsWith('/canvas/config')) return respond({user_id:'test-user',is_admin:true,models});
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
                return req.respond({status:200,contentType:record.mime,headers:{'X-Canvas-Revision':String(record.revision),'X-Canvas-Kind':record.kind},body:record.kind==='blob'?image:record.body});
            }
            if(path.endsWith('/canvas/generate')) {
                const payload=JSON.parse(req.postData());generations.push(payload);
                const id=`canvas-task-${generations.length}`;
                tasks.set(id,{id,status:payload.expected_kind==='video'?'queued':'succeeded',result_payload:payload.expected_kind==='text'?{text:'已参考当前人物图：保持人物身份，仅改变服装颜色。'}:{media_url:'/test-canvas-result.png'}});
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
        await page.goto('http://127.0.0.1:5173/canvas',{waitUntil:'networkidle0'});
        let frame = page.frames().find(frame=>frame.url().includes('/canvas-app/'));
        assert(frame,'Canvas iframe missing');
        await frame.waitForFunction(()=>document.body.textContent.includes('新画布'));
        async function clickText(text) {
            const buttons=await frame.$$('button');
            for (const button of buttons) {
                if ((await button.evaluate(el=>el.textContent.replace(/\s/g,'')))===text.replace(/\s/g,'')) {await button.click();return;}
            }
            throw new Error(`Button missing: ${text}`);
        }
        await clickText('模型设置');
        await frame.waitForFunction(()=>document.body.textContent.includes('自定义测试渠道'));
        assert(await frame.evaluate(()=>document.body.textContent.includes('系统管理')),'Managed channel must be protected');
        await page.screenshot({path:`.logs/infinite-canvas-config-${width}.png`,fullPage:true});
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
        await page.reload({waitUntil:'networkidle0'});
        frame=page.frames().find(frame=>frame.url().includes('/canvas-app/'));
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
        await frame.type('[contenteditable="true"]','生成蓝色花朵');
        await frame.click('button[aria-label="生成"]');
        await frame.waitForFunction(()=>!document.querySelector('button[aria-label="停止生成"]'));
        assert(generations.some(item=>item.expected_kind==='image' && item.model_id==='model-image' && item.prompt==='生成蓝色花朵'));
        await frame.click('.canvas-creation-toolbar button[aria-label="视频"]');
        await frame.waitForSelector('[contenteditable="true"]');
        await frame.type('[contenteditable="true"]','摄影机缓慢推近花朵');
        await frame.click('button[aria-label="生成"]');
        await frame.waitForSelector('button[aria-label="停止生成"]');
        for(let i=0;i<50 && !generations.some(item=>item.expected_kind==='video');i++) await new Promise(resolve=>setTimeout(resolve,100));
        const video=generations.find(item=>item.expected_kind==='video');
        assert(video && video.model_id==='model-video' && video.aspect_ratio==='16:9' && video.audio_enabled===false && [4,6,12].includes(video.duration_seconds),JSON.stringify(video));
        await frame.click('button[aria-label="停止生成"]');
        await frame.waitForSelector('.ant-modal-confirm');
        await frame.click('.ant-modal-confirm .ant-btn-dangerous');
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
        await page.goto('http://127.0.0.1:5173/canvas?mode=new#agentUrl=http%3A%2F%2F127.0.0.1%3A5173%2Fapi%2Fv1%2Fcanvas%2Fagent-test&agentToken=fragment-test',{waitUntil:'networkidle0'});
        frame=page.frames().find(frame=>frame.url().includes('/canvas-app/'));
        await frame.waitForSelector('.canvas-editor-layout');
        await frame.waitForFunction(()=>localStorage.getItem('canvas-agent-token:test-user')==='fragment-test');
        assert(!page.url().includes('agentToken'),'Host must remove connection credentials from address');
        assert(!frame.url().includes('agentToken'),'Canvas must remove handoff fragment after receipt');
        assert.equal(await frame.evaluate(()=>new URLSearchParams(location.hash.split('?')[1]).get('mode')),'new','Handoff must preserve create-canvas mode');
        console.log(`${width}px: canvas, Codex panel, custom channels/scripts, prompt sources, assets, assistant context, save/reload and generation parameters passed (${storage.size} records)`);
        await page.close();
    }
} finally { await browser.close(); }
