import { bootstrap } from './cineforge/bootstrap';

void bootstrap().then(() => import('./cineforge/render')).catch((error) => {
    document.getElementById('root')!.textContent = `画布加载失败：${String(error)}。请刷新重试。`;
});
