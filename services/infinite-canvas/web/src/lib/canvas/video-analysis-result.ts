import type { CanvasVideoAnalysis, CanvasVideoSegment } from '@/types/canvas';

export function parseVideoTimeline(text: string, analysis: CanvasVideoAnalysis) {
    const raw = JSON.parse(text.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, ''));
    if (typeof raw.summary !== 'string' || !Array.isArray(raw.segments) || !raw.segments.length) throw new Error('视频解析必须包含概述和连续时间轴');
    let cursor = analysis.evidence.start;
    const segments: CanvasVideoSegment[] = raw.segments.map((segment: CanvasVideoSegment) => {
        if (!segment || !Number.isFinite(segment.start) || !Number.isFinite(segment.end) || segment.end <= segment.start || Math.abs(segment.start - cursor) > 0.05 || segment.end > analysis.evidence.end + 0.05) throw new Error('视频解析时间轴缺失、重叠或超出原视频范围');
        for (const field of ['description','action','camera','lighting','videoPrompt'] as const) {
            if (typeof segment[field] !== 'string' || !segment[field].trim()) throw new Error(`视频解析缺少 ${field}`);
        }
        if (!Array.isArray(segment.frameTimes) || !segment.frameTimes.length) throw new Error('时间轴参考帧必须来自实际抽帧时间戳');
        const frameTimes = segment.frameTimes.map(at => {
            const frame = analysis.evidence.frames.find(frame => Number.isFinite(at) && Math.abs(frame.at - at) < 0.001);
            if (!frame || frame.at < segment.start || frame.at > segment.end) throw new Error('时间轴参考帧必须来自本段实际抽帧时间戳');
            return frame.at;
        });
        for (const frame of analysis.evidence.frames) {
            if (frame.at >= segment.start && frame.at < segment.end && ['before_transition','after_transition'].includes(frame.role || '')) frameTimes.push(frame.at);
        }
        cursor = segment.end;
        return { start: segment.start, end: segment.end, description: segment.description, action: segment.action, camera: segment.camera, lighting: segment.lighting, videoPrompt: segment.videoPrompt, frameTimes: [...new Set(frameTimes)].sort((a,b) => a-b) };
    });
    if (Math.abs(cursor - analysis.evidence.end) > 0.05) throw new Error('视频解析未覆盖完整分析范围');
    return { summary: raw.summary, segments };
}

export function videoTimelineText(analysis: CanvasVideoAnalysis) {
    const timeline = analysis.timeline;
    if (!timeline) return '';
    return `${timeline.summary}\n\n${timeline.segments.map(s => `${s.start}–${s.end}秒\n场景：${s.description}\n动作：${s.action}\n运镜：${s.camera}\n光线：${s.lighting}\n参考帧时间：${s.frameTimes.join('、')}秒\n复刻提示词：${s.videoPrompt}`).join('\n\n')}`;
}
