import { nanoid } from 'nanoid';
import { hostApi } from './api';
import type { AiConfig } from '@/stores/use-config-store';
import type { ReferenceImage } from '@/types/image';
import type { AiTextMessage } from '@/services/api/image';
import { imageToDataUrl, uploadImage } from '@/services/image-storage';
import { uploadMediaFile } from '@/services/file-storage';
import { inferMediaRatio, inferMediaScale } from '@/lib/media-size';

type Options = { signal?: AbortSignal };
type Task = { id: string; status: string; error_message?: string; result_payload: { text?: string; media_url?: string } };
type ReferenceKey = { namespace: string; key: string; purpose: string };
function modelId(config: AiConfig, type: 'image' | 'video' | 'text' | 'audio') {
    return String(config.model || config[`${type}Model`]).split('::').pop();
}
async function referenceKeys(references: ReferenceImage[], options?: Options): Promise<ReferenceKey[]> {
    return Promise.all(references.map(async image => {
        const stored = image.storageKey ? image : await uploadImage(await imageToDataUrl(image), options);
        return { namespace: 'infinite-canvas.image_files', key: stored.storageKey!, purpose: image.name || '参考图片' };
    }));
}
async function queue(config: AiConfig, kind: 'image' | 'video' | 'text' | 'audio', prompt: string,
                     references: ReferenceKey[], options?: Options) {
    const res = inferMediaScale(config.size).toUpperCase();
    const resolution = String(config.vquality || '720');
    return hostApi<{ id: string }>('/canvas/generate', { method: 'POST', signal: options?.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_id: modelId(config, kind), expected_kind: kind === 'audio' ? 'tts' : kind, prompt,
            reference_keys: references, aspect_ratio: inferMediaRatio(config.size) === 'auto' ? (kind === 'video' ? '16:9' : '1:1') : inferMediaRatio(config.size),
            resolution: kind === 'video' ? (/^\d+p?$/.test(resolution) ? resolution.replace(/p$/, '') + 'p' : resolution) : res === 'AUTO' ? '1K' : res,
            video_mode: config.videoMode === 'reference' ? 'reference' : 'frames',
            duration_seconds: Number(config.videoSeconds) || 6, audio_enabled: config.videoGenerateAudio !== 'false',
            voice: config.audioVoice || 'alloy', instructions: config.audioInstructions || '' }) });
}
export async function canvasTask(id: string, options?: Options) {
    return hostApi<Task>(`/tasks/${encodeURIComponent(id)}`, { signal: options?.signal });
}
export function cancelCanvasTask(id: string) {
    return hostApi(`/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
}
async function wait(id: string, options?: Options) {
    const abort = () => { void cancelCanvasTask(id).catch(() => {}); };
    options?.signal?.addEventListener('abort', abort, { once: true });
    try {
    const started = Date.now();
    while (Date.now() - started < 7_200_000) {
        options?.signal?.throwIfAborted();
        const task = await canvasTask(id, options);
        if (task.status === 'succeeded') return task.result_payload;
        if (task.status === 'failed' || task.status === 'cancelled') throw new Error(task.error_message || '画布生成失败');
        await new Promise<void>((resolve, reject) => {
            const abort = () => { clearTimeout(timer); reject(new DOMException('已取消', 'AbortError')); };
            const timer = setTimeout(() => { options?.signal?.removeEventListener('abort', abort); resolve(); }, 2000);
            options?.signal?.addEventListener('abort', abort, { once: true });
        });
    }
    throw new Error('任务仍未返回，请稍后在任务中心查看结果');
    } finally { options?.signal?.removeEventListener('abort', abort); }
}
export async function canvasImages(config: AiConfig, prompt: string, references: ReferenceImage[], options?: Options) {
    const refs = await referenceKeys(references, options);
    const count = Math.max(1, Math.min(15, Number(config.count) || 1));
    const results = [];
    for (let i = 0; i < count; i++) {
        options?.signal?.throwIfAborted();
        const task = await queue(config, 'image', prompt, refs, options);
        const result = await wait(task.id, options);
        if (!result.media_url) throw new Error('生图任务未返回图片');
        results.push({ id: nanoid(), dataUrl: result.media_url });
    }
    return results;
}
export async function canvasText(config: AiConfig, messages: AiTextMessage[], onDelta: (text: string) => void, options?: Options) {
    const refs: ReferenceImage[] = [];
    const history = messages.map(message => ({ role: message.role, content: typeof message.content === 'string' ? message.content
        : message.content.map(item => {
            if (item.type === 'text') return item.text;
            refs.push({ id: nanoid(), name: `对话参考图${refs.length + 1}`, type: 'image/png', dataUrl: item.image_url.url });
            return `[参考图${refs.length}]`;
        }).join('\n') }));
    const task = await queue(config, 'text', JSON.stringify(history), await referenceKeys(refs, options), options);
    const result = await wait(task.id, options);
    const text = result.text || '';
    onDelta(text);
    return text;
}
export async function canvasVideo(config: AiConfig, prompt: string, references: ReferenceImage[],
                                  options?: Options & { videos?: Array<{ url?: string; storageKey?: string; name?: string }>; audios?: Array<{ url?: string; storageKey?: string; name?: string }> }) {
    const refs = await referenceKeys(references, options);
    for (const ref of [...(options?.videos || []), ...(options?.audios || [])]) {
        const stored = ref.storageKey ? ref : await uploadMediaFile(ref.url || '', 'reference');
        refs.push({ namespace: 'infinite-canvas.media_files', key: stored.storageKey!, purpose: ref.name || '参考媒体' });
    }
    return queue(config, 'video', prompt, refs, options);
}
export async function canvasAudio(config: AiConfig, prompt: string, options?: Options) {
    const result = await wait((await queue(config, 'audio', prompt, [], options)).id, options);
    if (!result.media_url) throw new Error('配音任务未返回音频');
    return (await fetch(result.media_url, { signal: options?.signal })).blob();
}
