import { useState } from 'react';
import { App, Modal } from 'antd';
import type { CanvasNodeData } from '@/types/canvas';
import { CanvasNodeType } from '@/types/canvas';
import { processCanvasMedia } from '@/cineforge/media-processing';

export function CanvasAudioMuxModal({ video, nodes, onClose }: { video: CanvasNodeData; nodes: CanvasNodeData[]; onClose: () => void }) {
    const { message } = App.useApp();
    const audios = nodes.filter(node => node.type === CanvasNodeType.Audio && node.metadata?.content);
    const [audioNodeId, setAudioNodeId] = useState(audios.length === 1 ? audios[0].id : '');
    const [audioStart, setAudioStart] = useState(0);
    const [offset, setOffset] = useState(0);
    const [busy, setBusy] = useState(false);
    const submit = async () => {
        if (!audioNodeId || busy) return;
        setBusy(true);
        try { await processCanvasMedia(video.id, 'mux_audio', { audioNodeId, audioStart, offset }); message.success('已创建带音轨的新视频'); onClose(); }
        catch (error) { message.error(error instanceof Error ? error.message : String(error)); }
        finally { setBusy(false); }
    };
    const fieldClass = 'mt-2 min-h-11 w-full rounded-lg border border-current/15 bg-transparent px-3 text-sm';
    return <Modal title="封装音轨" open centered onCancel={onClose} onOk={() => void submit()} okText="生成新视频" cancelText="取消" confirmLoading={busy} okButtonProps={{ disabled: !audioNodeId }}>
        <div className="space-y-4 py-2" data-canvas-no-zoom>
            <p className="text-sm opacity-70">替换「{video.title}」的原声音，保持画面和视频时长。原素材保留。</p>
            <label className="block text-sm">选择画布音轨<select aria-label="选择画布音轨" className={fieldClass} value={audioNodeId} onChange={event => setAudioNodeId(event.target.value)}>
                <option value="">{audios.length ? '请选择音轨' : '请先上传音频或从视频提取音轨'}</option>
                {audios.map(audio => <option key={audio.id} value={audio.id}>{audio.title}</option>)}
            </select></label>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <label className="block text-sm">音频起点（秒）<input aria-label="音频起点（秒）" type="number" min="0" step="0.001" className={fieldClass} value={audioStart} onChange={event => setAudioStart(Number(event.target.value))} /></label>
                <label className="block text-sm">视频播放起点（秒）<input aria-label="视频播放起点（秒）" type="number" min="0" step="0.001" className={fieldClass} value={offset} onChange={event => setOffset(Number(event.target.value))} /></label>
            </div>
            <p className="text-xs leading-5 opacity-60">音频较短时剩余画面静音，较长时按视频时长截断，不叠加多条声音。</p>
        </div>
    </Modal>;
}
