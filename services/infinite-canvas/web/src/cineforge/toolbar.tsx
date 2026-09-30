import { useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { App, Button, Input, Modal, Select } from 'antd';
import { BookOpen, Boxes, FolderOpen, Plus, Settings2 } from 'lucide-react';
import { hostApi, notify } from './api';
import { useCanvasStore } from '@/stores/canvas/use-canvas-store';
import { useConfigStore } from '@/stores/use-config-store';
import { AppConfigModal } from '@/components/layout/app-config-modal';
import { useThemeStore } from '@/stores/use-theme-store';
import { createCanvasNode, imageMetadata } from '@/lib/canvas/canvas-node-factory';
import { CanvasNodeType } from '@/types/canvas';
import { uploadImage } from '@/services/image-storage';
import { useNavigate } from 'react-router-dom';
import { PlatformAssistant } from './assistant';
import { useAgentStore } from '@/stores/use-agent-store';

type Asset = { id: string; name: string; media_url: string | null; generation_prompt: string; description: string };
export function PlatformToolbar() {
    const location = useLocation();
    const navigate = useNavigate();
    const { message } = App.useApp();
    const [assetsOpen, setAssetsOpen] = useState(false);
    const [assets, setAssets] = useState<Asset[]>([]);
    const [projects, setProjects] = useState<Array<{ id: string; name: string }>>([]);
    const [project, setProject] = useState('global');
    const [search, setSearch] = useState('');
    const [busy, setBusy] = useState(false);
    const [assistantOpen, setAssistantOpen] = useState(false);
    const id = location.pathname.match(/^\/canvas\/(.+)$/)?.[1];
    useEffect(() => {
        const openAssistant = () => setAssistantOpen(true);
        const openSettings = () => useConfigStore.getState().openConfigDialog();
        window.addEventListener('cineforge:assistant', openAssistant);
        window.addEventListener('cineforge:settings', openSettings);
        return () => { window.removeEventListener('cineforge:assistant', openAssistant); window.removeEventListener('cineforge:settings', openSettings); };
    }, []);
    useEffect(() => {
        const sync = () => useThemeStore.getState().setTheme(localStorage.getItem('cineforge-theme') === 'light' ? 'light' : 'dark');
        const listener = (event: MessageEvent) => {
            if (event.origin === window.location.origin && event.source === window.parent && event.data?.source === 'cineforge-host' && event.data.theme) sync();
        };
        sync(); window.addEventListener('message', listener);
        return () => window.removeEventListener('message', listener);
    }, []);
    useEffect(() => {
        if (!assetsOpen) return;
        let alive = true;
        void hostApi<Asset[]>(project === 'global' ? '/assets' : `/projects/${project}/assets`).then(rows => { if (alive) setAssets(rows); }).catch(() => {});
        return () => { alive = false; };
    }, [assetsOpen, project]);
    async function addAsset(asset: Asset) {
        if (!id || !asset.media_url) return;
        setBusy(true);
        try {
            const image = await uploadImage(asset.media_url);
            const store = useCanvasStore.getState();
            const current = store.openProject(id);
            if (!current) throw new Error('画布不存在');
            const viewport = useAgentStore.getState().canvasContext?.snapshot.viewport || current.viewport;
            const surface = document.querySelector('.canvas-editor-layout > section')?.getBoundingClientRect();
            const node = createCanvasNode(CanvasNodeType.Image, { x: ((surface?.width || 640) / 2 - viewport.x) / viewport.k,
                y: ((surface?.height || 480) / 2 - viewport.y) / viewport.k }, imageMetadata(image));
            node.title = asset.name;
            node.metadata!.prompt = asset.generation_prompt || asset.description;
            window.dispatchEvent(new CustomEvent('cineforge:add-node', { detail: { projectId: id, node } }));
            setAssetsOpen(false);
            message.success('资产已加入画布，可连线作为参考图');
        } catch (error) { message.error(String(error)); } finally { setBusy(false); }
    }
    return <>
        {id && <PlatformAssistant key={id} projectId={id} open={assistantOpen} onClose={() => setAssistantOpen(false)} />}
        <header className="platform-canvas-toolbar">
            <Link to="/canvas" className="platform-canvas-brand"><span className="platform-canvas-mark">∞</span><span>无限画布<small>Powered by infinite-canvas · basketikun</small></span></Link>
            <div className="platform-canvas-actions">
                <Button icon={<FolderOpen size={16} />} onClick={() => navigate('/canvas')}>画布库</Button>
                <Button icon={<Plus size={16} />} onClick={() => navigate(`/canvas/${useCanvasStore.getState().createProject()}`)}>新画布</Button>
                <Button disabled={!id} icon={<Boxes size={16} />} onClick={() => {
                    setAssetsOpen(true); void hostApi<Array<{ id: string; name: string }>>('/projects').then(setProjects).catch(() => {});
                }}>资产库</Button>
                <Button icon={<Settings2 size={16} />} onClick={() => navigate('/config')}>模型设置</Button>
                <Button icon={<BookOpen size={16} />} onClick={() => navigate('/prompts')}>模板中心</Button>
            </div>
        </header>
        <Modal title="从资产库加入画布" open={assetsOpen} onCancel={() => setAssetsOpen(false)} footer={null} width={720}>
            <div className="platform-canvas-library-tools"><Select value={project} onChange={setProject} options={[
                { value: 'global', label: '我的全局资产' }, ...projects.map(p => ({ value: p.id, label: p.name }))]} />
                <Input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索人物、场景、道具…" /></div>
            <div className="platform-canvas-asset-grid">{assets.filter(a => a.media_url && a.name.includes(search)).map(asset =>
                <button key={asset.id} disabled={busy} onClick={() => void addAsset(asset)}><img src={asset.media_url!} alt={asset.name} loading="lazy" /><span>{asset.name}</span></button>)}</div>
            {!assets.some(a => a.media_url && a.name.includes(search)) && <p>这里还没有可用的资产图片。</p>}
        </Modal>
        <AppConfigModal />
    </>;
}
