import { useEffect, useState } from 'react';
import { App } from 'antd';
import { Film, Images, Video } from 'lucide-react';
import type { CanvasNodeData } from '@/types/canvas';
import { resolveImageUrl } from '@/services/image-storage';
import { useAgentStore } from '@/stores/use-agent-store';
import { createVideoReplicaConfig } from '@/cineforge/video-analysis';

export function CanvasVideoAnalysisContent({ node }: { node: CanvasNodeData }) {
    const { message } = App.useApp();
    const analysis = node.metadata!.videoAnalysis!;
    const timeline = analysis.timeline!;
    const [images, setImages] = useState<Record<string,string>>({});
    const [preparing, setPreparing] = useState<number | null>(null);
    const createReplica = async (index: number) => {
        if (preparing !== null) return;
        setPreparing(index);
        try { await createVideoReplicaConfig(node.id,index); message.success('复刻配置已创建，已关联时间轴及实际参考素材'); }
        catch (error) { message.error(error instanceof Error ? error.message : String(error)); }
        finally { setPreparing(null); }
    };
    useEffect(() => {
        let active = true;
        const times = timeline.segments.flatMap(s => s.frameTimes);
        void Promise.all(analysis.evidence.frames.filter(f => times.includes(f.at)).map(async frame => [frame.storageKey, await resolveImageUrl(frame.storageKey, '')] as const))
            .then(entries => { if (active) setImages(Object.fromEntries(entries)); }).catch(() => {});
        return () => { active = false; };
    }, [analysis, timeline]);
    return <div className="thin-scrollbar h-full w-full overflow-y-auto p-4 text-sm" data-canvas-no-zoom data-video-analysis-timeline onPointerDown={event => event.stopPropagation()}>
        <div className="mb-3 flex items-center gap-2 font-semibold"><Film className="size-4" />视频解析<span className="ml-auto text-xs font-normal opacity-60 tabular-nums">{analysis.evidence.start}–{analysis.evidence.end}s</span></div>
        <p className="mb-3 whitespace-pre-wrap text-xs leading-5 opacity-80">{timeline.summary}</p>
        {!!analysis.evidence.transition_count && <p className="mb-2 text-xs leading-5 opacity-70">已保留 {analysis.evidence.transitions?.length || 0}/{analysis.evidence.transition_count} 处画面变化的前后状态{(analysis.evidence.transitions?.length || 0) < analysis.evidence.transition_count ? '；未覆盖处需要按时间补取后再复刻。' : '，复刻将保留原片转场节奏。'}</p>}
        <button type="button" className="mb-4 inline-flex min-h-10 items-center gap-2 text-xs hover:opacity-70" onClick={() => useAgentStore.getState().canvasContext?.applyOps([{ type: 'select_nodes', ids: [analysis.groupNodeId] }])}><Images className="size-4" />查看全部 {analysis.evidence.frames.length} 张关键帧</button>
        <div className="space-y-4">{timeline.segments.map((segment,index) => <article key={index} className="border-t border-current/10 pt-3">
            <div className="mb-2 flex items-center justify-between gap-3"><span className="font-semibold tabular-nums">{segment.start}–{segment.end}s</span><button type="button" disabled={preparing !== null} className="inline-flex min-h-10 items-center gap-1 text-xs hover:opacity-70 disabled:opacity-50" onClick={() => void createReplica(index)}><Video className="size-3.5" />{preparing === index ? '准备参考素材…' : '创建复刻配置'}</button></div>
            <div className="mb-2 flex gap-2 overflow-x-auto">{analysis.evidence.frames.filter(f => segment.frameTimes.includes(f.at)).map(frame => <figure key={frame.storageKey} className="w-24 shrink-0"><img src={images[frame.storageKey]} alt={`${frame.at}s 原片关键帧`} className="h-16 w-full rounded-md object-contain outline outline-1 -outline-offset-1 outline-black/10 dark:outline-white/10" /><figcaption className="mt-1 text-center text-[10px] opacity-60 tabular-nums">{frame.at}s</figcaption></figure>)}</div>
            <p className="text-xs leading-5">{segment.description}</p><p className="mt-1 text-xs leading-5 opacity-80">{segment.action}</p><p className="mt-1 text-xs leading-5 opacity-60">{segment.camera} · {segment.lighting}</p>
            <details className="mt-2 text-xs"><summary className="cursor-pointer py-2">复刻提示词</summary><p className="whitespace-pre-wrap leading-5">{segment.videoPrompt}</p></details>
        </article>)}</div>
    </div>;
}
