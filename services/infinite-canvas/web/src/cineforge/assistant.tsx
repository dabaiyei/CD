import { useEffect, useRef, useState } from 'react';
import { App, Button, Drawer, Input } from 'antd';
import localforage from 'localforage';
import { nanoid } from 'nanoid';
import { useConfigStore } from '@/stores/use-config-store';
import { useAgentStore } from '@/stores/use-agent-store';
import { createCanvasNode } from '@/lib/canvas/canvas-node-factory';
import { CanvasNodeType } from '@/types/canvas';
import { imageToDataUrl } from '@/services/image-storage';
import { canvasText } from './generation';
import type { AiTextMessage } from '@/services/api/image';

const store = localforage.createInstance({ name: 'infinite-canvas', storeName: 'assistant_messages' });
type Message = { id: string; role: 'user' | 'assistant'; content: string };
export function PlatformAssistant({ projectId, open, onClose }: { projectId: string; open: boolean; onClose: () => void }) {
    const config = useConfigStore(state => state.config);
    const [messages, setMessages] = useState<Message[]>([]);
    const [prompt, setPrompt] = useState('');
    const [busy, setBusy] = useState(false);
    const [loaded, setLoaded] = useState(false);
    const controller = useRef<AbortController | null>(null);
    const { message } = App.useApp();
    useEffect(() => {
        let active = true;
        setLoaded(false);
        void store.getItem<Message[]>(projectId).then(value => { if (active) { setMessages(value || []); setLoaded(true); } }).catch(error => message.error(String(error)));
        return () => { active = false; controller.current?.abort(); };
    }, [projectId]);
    async function send() {
        if (!prompt.trim() || busy || !loaded) return;
        const context = useAgentStore.getState().canvasContext;
        if (context?.snapshot.projectId !== projectId) return;
        const snapshot = context.snapshot;
        const selected = snapshot.nodes.filter(node => snapshot.selectedNodeIds.includes(node.id));
        const content = prompt.trim();
        const next: Message[] = [...messages, { id: nanoid(), role: 'user', content }];
        const instructions = [{ type: 'text' as const, text: content }];
        setBusy(true);
        controller.current = new AbortController();
        try {
            for (const node of selected.slice(0, 8)) {
                instructions.push({ type: 'text', text: `节点 ${node.title}：${String(node.metadata?.prompt || node.metadata?.content || '').slice(0, 2000)}` });
            }
            const inputs: Exclude<AiTextMessage['content'], string> = [...instructions];
            for (const node of selected.filter(node => node.type === CanvasNodeType.Image).slice(0, 5)) {
                const url = await imageToDataUrl({ storageKey: node.metadata?.storageKey, dataUrl: node.metadata?.content || '' });
                inputs.push({ type: 'image_url', image_url: { url } });
            }
            const history: AiTextMessage[] = messages.slice(-12).map(item => ({ role: item.role, content: item.content }));
            history.push({ role: 'user', content: inputs });
            // Only this canvas's recent conversation and explicitly selected nodes are sent.
            await store.setItem(projectId, next);
            setMessages(next); setPrompt('');
            const answer = await canvasText({ ...config, model: config.textModel }, history, () => {}, { signal: controller.current.signal });
            const complete: Message[] = [...next, { id: nanoid(), role: 'assistant', content: answer }];
            await store.setItem(projectId, complete);
            setMessages(complete);
        } catch (error) { message.error(String(error)); } finally { setBusy(false); }
    }
    function addText(content: string) {
        const viewport = useAgentStore.getState().canvasContext?.snapshot.viewport || {x:0,y:0,k:1};
        const surface = document.querySelector('.canvas-editor-layout > section')?.getBoundingClientRect();
        const node = createCanvasNode(CanvasNodeType.Text, { x:((surface?.width || 640)/2-viewport.x)/viewport.k,y:((surface?.height || 480)/2-viewport.y)/viewport.k }, {content,status:'success'});
        window.dispatchEvent(new CustomEvent('cineforge:add-node', { detail:{projectId,node} }));
        message.success('已加入文本节点，可连线继续生成');
    }
    return <Drawer title="画布助手" open={open} onClose={onClose} size={Math.min(420, window.innerWidth)} styles={{body:{padding:16,display:'flex',flexDirection:'column',gap:16,minHeight:0}}}>
        <p className="platform-assistant-hint">选中人物图或文本节点后提问，助手会参考这些节点。回复可以加入画布继续创作。</p>
        <div className="platform-assistant-messages">{messages.map(item => <article key={item.id} className={`platform-assistant-message ${item.role}`}><small>{item.role === 'user' ? '你' : '画布助手'}</small><p>{item.content}</p>{item.role === 'assistant' && <Button size="small" onClick={() => addText(item.content)}>加入文本节点</Button>}</article>)}{busy && <p role="status">正在分析当前画布…</p>}</div>
        <Input.TextArea value={prompt} onChange={event=>setPrompt(event.target.value)} autoSize={{minRows:3,maxRows:6}} placeholder="描述想法、分析选中图片或完善生成提示词…" disabled={!loaded} />
        <div style={{display:'flex',justifyContent:'flex-end',gap:8}}>{busy && <Button onClick={()=>controller.current?.abort()}>停止</Button>}<Button type="primary" disabled={busy || !loaded || !prompt.trim()} onClick={()=>void send()}>发送</Button></div>
    </Drawer>;
}
