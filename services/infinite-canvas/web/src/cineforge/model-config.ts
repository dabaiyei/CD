import type { AiConfig } from '@/stores/use-config-store';
import type { CanvasNodeMetadata } from '@/types/canvas';
import { computeMediaSize, inferMediaRatio, inferMediaScale } from '@/lib/media-size';

// Choose valid inherited defaults without changing explicitly configured node parameters.
export function platformNodeConfig(config: AiConfig, mode: string, metadata?: CanvasNodeMetadata): AiConfig {
    const model = window.cineforgeCanvas?.models.find(m => `cineforge::${m.id}` === config.model);
    if (!model || !['image', 'video'].includes(mode)) return config;
    const next = { ...config };
    const caps = model.capabilities || {};
    const ratios: string[] = caps.aspect_ratios || [];
    const ratio = inferMediaRatio(next.size);
    if (!metadata?.size && ratios.length && !ratios.includes(ratio)) {
        next.size = mode === 'video' ? ratios[0] : computeMediaSize(inferMediaScale(next.size), ratios[0]) || ratios[0];
    }
    if (mode === 'video') {
        const durations: number[] = caps.duration_resolution_map?.flatMap((group: any) => group.durations) || caps.durations || [];
        if (!metadata?.seconds && durations.length && !durations.includes(Number(next.videoSeconds))) next.videoSeconds = String(durations[0]);
        const resolutions: string[] = caps.duration_resolution_map?.filter((group: any) => group.durations.includes(Number(next.videoSeconds))).flatMap((group: any) => group.resolutions) || caps.resolutions || [];
        const resolution = /^\d+p?$/.test(next.vquality) ? next.vquality.replace(/p$/, '') + 'p' : next.vquality;
        if (!metadata?.vquality && resolutions.length && !resolutions.includes(resolution)) next.vquality = resolutions[0];
        if (caps.audio_policy === 'disabled') next.videoGenerateAudio = 'false';
        if (caps.audio_policy === 'required') next.videoGenerateAudio = 'true';
    } else {
        const resolutions: string[] = caps.resolutions || [];
        if (!metadata?.size && resolutions.length && !resolutions.includes(inferMediaScale(next.size).toUpperCase())) {
            next.size = computeMediaSize(resolutions[0].toLowerCase(), inferMediaRatio(next.size)) || next.size;
        }
    }
    return next;
}
