import { Select, Switch } from 'antd';
import type { AiConfig } from '@/stores/use-config-store';
import { decodeChannelModel } from '@/stores/use-config-store';
import { inferMediaRatio, inferMediaScale, computeMediaSize } from '@/lib/media-size';

export function canvasModel(config: AiConfig, kind: string) {
    const selected = decodeChannelModel(String(config.model || config[`${kind}Model` as keyof AiConfig] || ''));
    if (selected?.channelId !== 'cineforge') return undefined;
    return window.cineforgeCanvas?.models.find(model => model.id === selected.model && model.type === (kind === 'audio' ? 'tts' : kind));
}
export function durations(caps: Record<string, any>) {
    return [...new Set<number>(caps.duration_resolution_map?.flatMap((group: any) => group.durations) || caps.durations || [])].sort((a, b) => a - b);
}
export function PlatformVideoSettings({ config, onChange }: { config: AiConfig; onChange: (key: 'vquality' | 'size' | 'videoSeconds' | 'videoGenerateAudio' | 'videoMode', value: string) => void }) {
    const caps = canvasModel(config, 'video')?.capabilities || {};
    const seconds = durations(caps);
    const resolutions: string[] = [...new Set<string>(caps.duration_resolution_map?.filter((group: any) => group.durations.includes(Number(config.videoSeconds))).flatMap((group: any) => group.resolutions) || caps.resolutions || ['720p'])];
    return <div className="platform-canvas-settings">
        <p>按当前视频模型的能力生成；连线素材会作为参考传入。</p>
        <label>视频时长<Select value={Number(config.videoSeconds)} options={seconds.map(value => ({ value, label: `${value} 秒` }))} onChange={value => onChange('videoSeconds', String(value))} /></label>
        <label>分辨率<Select value={config.vquality.replace(/p$/, '') + (/^\d+p?$/.test(config.vquality) ? 'p' : '')} options={resolutions.map(value => ({ value, label: value }))} onChange={value => onChange('vquality', value)} /></label>
        <label>画幅<Select value={config.size.includes(':') ? config.size : inferMediaRatio(config.size)} options={(caps.aspect_ratios || ['16:9', '9:16', '1:1']).map((value: string) => ({ value, label: value }))} onChange={value => onChange('size', value)} /></label>
        <label>参考方式<Select value={config.videoMode || 'frames'} options={[{value: 'frames', label: '首帧 / 首尾帧'}, {value: 'reference', label: '角色与场景参考'}]} onChange={value => onChange('videoMode', value)} /></label>
        <label>生成声音<Switch disabled={caps.audio_policy === 'disabled' || caps.audio_policy === 'required'} checked={caps.audio_policy === 'required' || (caps.audio_policy !== 'disabled' && config.videoGenerateAudio !== 'false')} onChange={value => onChange('videoGenerateAudio', String(value))} /></label>
        <small>{Object.entries(caps.reference_limits || {}).map(([type, limit]: [string, any]) => `${{image:'图片',video:'视频',audio:'音频'}[type as 'image'] || type}：${limit.enabled ? `最多 ${limit.max_count} 个` : '不支持'}`).join(' · ')}</small>
    </div>;
}
export function PlatformImageSettings({ config, onChange }: { config: AiConfig; onChange: (key: 'quality' | 'size' | 'count' | 'background', value: string) => void }) {
    const caps = canvasModel(config, 'image')?.capabilities || {};
    const ratio = inferMediaRatio(config.size);
    const scale = inferMediaScale(config.size).toUpperCase();
    return <div className="platform-canvas-settings">
        <p>提示词和参考图按原意提交，使用系统配置的图片模型。</p>
        <label>画幅<Select value={ratio} options={(caps.aspect_ratios || ['1:1','16:9','9:16','4:3','3:4','2:3','3:2']).map((value: string) => ({value, label:value}))} onChange={value => onChange('size', computeMediaSize(scale === 'AUTO' ? '1k' : scale.toLowerCase(), value) || value)} /></label>
        <label>分辨率<Select value={scale === 'AUTO' ? '1K' : scale} options={(caps.resolutions || ['1K','2K','4K']).map((value: string) => ({value,label:value}))} onChange={value => onChange('size', computeMediaSize(value.toLowerCase(), ratio === 'auto' ? '1:1' : ratio) || config.size)} /></label>
        <label>图片数量<Select value={Number(config.count) || 1} options={[1,2,3,4].map(value => ({value,label:`${value} 张`}))} onChange={value => onChange('count', String(value))} /></label>
    </div>;
}
