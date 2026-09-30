import assert from 'node:assert/strict';
import test from 'node:test';
import { parseVideoTimeline, videoTimelineText } from '../services/infinite-canvas/web/src/lib/canvas/video-analysis-result';
import type { CanvasVideoAnalysis } from '../services/infinite-canvas/web/src/types/canvas';

const analysis: CanvasVideoAnalysis = { sourceNodeId:'source', groupNodeId:'frames', referenceNodeIds:['replacement'],
    evidence:{duration:12,start:3,end:12,has_audio:false,source_key:'video:source',source_revision:1,
        frames:[{at:3.25,nodeId:'frame-a',storageKey:'image:a'},{at:7.5,nodeId:'frame-b',storageKey:'image:b'},{at:11.75,nodeId:'frame-c',storageKey:'image:c'}],sheets:[{storageKey:'image:sheet'}]} };
const segment={start:3,end:12,description:'保持原片空间',action:'举手→转身→冲刺，动作不断',camera:'同步跟拍后推近眼神',lighting:'逆光',videoPrompt:'参考图1保持动作，参考图2替换身份',frameTimes:[3.25,7.5,11.75]};
const result={summary:'连续九秒动作；声音未知',segments:[segment]};

test('模型漏选转场参考时仍保留本段真实前后状态',()=>{
    const evidence = {...analysis.evidence, frames:[...analysis.evidence.frames,
        {at:5,nodeId:'before',storageKey:'image:before',role:'before_transition'},
        {at:5.1,nodeId:'after',storageKey:'image:after',role:'after_transition'}]};
    const timeline=parseVideoTimeline(JSON.stringify(result), {...analysis,evidence});
    assert.deepEqual(timeline.segments[0].frameTimes,[3.25,5,5.1,7.5,11.75]);
});

test('完整时间轴保留动作、运镜、身份关系与真实参考时间戳',()=>{
    const timeline=parseVideoTimeline('```json\n'+JSON.stringify(result)+'\n```',analysis);
    assert.deepEqual(timeline,result);
    const text=videoTimelineText({...analysis,timeline});
    for(const expected of ['3–12秒','举手→转身→冲刺','同步跟拍后推近眼神','3.25、7.5、11.75','替换身份']) assert(text.includes(expected));
});

test('不能把缺失范围、重叠时间轴或杜撰的参考帧当成成功结果',()=>{
    for(const patch of [{start:4},{end:11},{end:13},{frameTimes:[4]},{frameTimes:[]},{action:''}]) {
        assert.throws(()=>parseVideoTimeline(JSON.stringify({...result,segments:[{...segment,...patch}]}),analysis));
    }
    assert.throws(()=>parseVideoTimeline(JSON.stringify({...result,segments:[{...segment,end:8},{...segment,start:7}]}),analysis));
    assert.throws(()=>parseVideoTimeline('已分析完成',analysis));
});

test('采样盲区不拆成视频任务，多个真实叙事片段仍保持连续范围',()=>{
    const timeline=parseVideoTimeline(JSON.stringify({...result,segments:[{...segment,end:8,frameTimes:[3.25,7.5]},{...segment,start:8,frameTimes:[11.75]}]}),analysis);
    assert.equal(timeline.segments.length,2);
    assert.equal(timeline.segments[0].end,timeline.segments[1].start);
});

test('轻微时间戳取整仍匹配真实图片，不能引用另一段的画面',()=>{
    const timeline=parseVideoTimeline(JSON.stringify({...result,segments:[{...segment,frameTimes:[11.7501,3.2501,7.5,3.25]}]}),analysis);
    assert.deepEqual(timeline.segments[0].frameTimes,[3.25,7.5,11.75]);
    assert.throws(()=>parseVideoTimeline(JSON.stringify({...result,segments:[{...segment,end:8,frameTimes:[11.75]},{...segment,start:8,frameTimes:[11.75]}]}),analysis));
    assert.throws(()=>parseVideoTimeline(JSON.stringify({...result,segments:[{...segment,frameTimes:['7.5']}]}),analysis));
});
