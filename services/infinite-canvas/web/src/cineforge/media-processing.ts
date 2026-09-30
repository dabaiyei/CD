import { nanoid } from 'nanoid';
import { hostApi } from './api';
import { resolveMediaUrl, uploadMediaFile } from '@/services/file-storage';
import { useAgentStore } from '@/stores/use-agent-store';
import { CanvasNodeType } from '@/types/canvas';

type MediaResult = { storageKey: string; mimeType: string; bytes: number; duration: number; width?: number; height?: number; source_start: number; source_end: number };
const processing = new Set<string>();
type MediaOperation = 'extract_audio' | 'mux_audio' | 'trim_video';
type MediaOptions = { audioNodeId?: string; start?: number; end?: number; audioStart?: number; offset?: number };

export async function processCanvasMedia(nodeId: string, operation: MediaOperation, options: MediaOptions = {}) {
    return await createMediaJob(nodeId, operation, options).promise;
}

/** Return immediately to Codex; generation_get_status reads the resulting media node. */
export function startCanvasMedia(nodeId: string, operation: MediaOperation, options: MediaOptions = {}) {
    const job = createMediaJob(nodeId, operation, options);
    void job.promise.catch(() => {}); // Failure is stored on the visible output node.
    return { nodeId: job.nodeId, status: 'running', message: '本地媒体处理已启动，用 generation_get_status 查询此节点；完成后可播放或下载。' };
}

function createMediaJob(nodeId: string, operation: MediaOperation, options: MediaOptions) {
    const key = `${useAgentStore.getState().canvasContext?.snapshot.projectId}:${nodeId}:${operation}:${JSON.stringify(options)}`;
    if (processing.has(key)) throw new Error('此媒体操作正在进行，请等待当前处理完成');
    const context = useAgentStore.getState().canvasContext;
    const video = context?.snapshot.nodes.find(n => n.id === nodeId && n.type === CanvasNodeType.Video);
    if (!context || !video?.metadata?.content) throw new Error('请选择有内容的视频节点');
    const videoMetadata = video.metadata;
    const videoContent = video.metadata.content;
    const connected = context.snapshot.connections.filter(c => c.toNodeId === nodeId).map(c => c.fromNodeId);
    const audios = context.snapshot.nodes.filter(n => n.type === CanvasNodeType.Audio && n.metadata?.content &&
        (options.audioNodeId ? n.id === options.audioNodeId : connected.includes(n.id)));
    if (operation === 'mux_audio' && audios.length !== 1) throw new Error('请将一条音轨连入视频，或明确指定要使用的音轨节点');
    const audio = operation === 'mux_audio' ? audios[0] : undefined;
    const id = `${operation}-${nanoid()}`;
    context.applyOps([
        { type: 'add_node', id, nodeType: operation === 'extract_audio' ? CanvasNodeType.Audio : CanvasNodeType.Video,
            title: `${video.title} · ${operation === 'extract_audio' ? '原片音轨' : operation === 'mux_audio' ? '已封装音轨' : `${options.start || 0}–${options.end ?? '结束'}s 参考片段`}`,
            x: video.position.x + video.width + 80, y: video.position.y + video.height + 80,
            ...(operation !== 'extract_audio' ? { width: video.width, height: video.height } : {}),
            metadata: { status: 'loading', mediaSource: { nodeId, operation, start: options.start || 0, end: options.end ?? (video.metadata.durationMs || 0)/1000, audioNodeId: audio?.id, audioStart: options.audioStart || 0, offset: options.offset || 0 } } },
        { type: 'connect_nodes', fromNodeId: nodeId, toNodeId: id },
        ...(audio ? [{ type: 'connect_nodes' as const, fromNodeId: audio.id, toNodeId: id }] : []),
        { type: 'select_nodes', ids: [id] },
    ]);
    processing.add(key);
    const promise = (async () => {
        try {
            const videoKey = videoMetadata.storageKey || (await uploadMediaFile(videoContent, 'source-video')).storageKey;
            const audioKey = audio ? audio.metadata?.storageKey || (await uploadMediaFile(audio.metadata!.content!, 'source-audio')).storageKey : undefined;
            const result = await hostApi<MediaResult>('/canvas/media-process', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ operation, video_key: videoKey, audio_key: audioKey, start: options.start, end: options.end, audio_start: options.audioStart, offset: options.offset }) });
            const url = await resolveMediaUrl(result.storageKey);
            if (!url) throw new Error('处理结果已保存，但未能读取媒体文件，请刷新画布后重试');
            if (useAgentStore.getState().canvasContext?.snapshot.projectId !== context.snapshot.projectId) throw new Error('画布已切换，处理结果已保存到当前账号素材存储');
            context.applyOps([{ type: 'update_node', id,
                metadata: { content: url, storageKey: result.storageKey, mimeType: result.mimeType, bytes: result.bytes, durationMs: Math.round(result.duration * 1000),
                    naturalWidth: result.width, naturalHeight: result.height, status: 'success',
                    mediaSource: { nodeId, operation, start: result.source_start, end: result.source_end, audioNodeId: audio?.id, audioStart: options.audioStart || 0, offset: options.offset || 0 } } }]);
            return { nodeId: id, ...result };
        } catch (error) {
            if (useAgentStore.getState().canvasContext?.snapshot.projectId === context.snapshot.projectId) context.applyOps([{type:'update_node', id, metadata:{status:'error',errorDetails:error instanceof Error ? error.message : String(error)}}]);
            throw error;
        } finally { processing.delete(key); }
    })();
    return { nodeId: id, promise };
}
