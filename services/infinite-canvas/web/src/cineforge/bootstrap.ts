import localforage from 'localforage';
import { hostApi, hostFetch, notify, type PlatformContext } from './api';

const revisions = new Map<string, number>();
const pending = new Map<string, Promise<unknown>>();
function path(namespace: string, key?: string) {
    return `/canvas/storage/${encodeURIComponent(namespace)}${key === undefined ? '' : `/${encodeURIComponent(key)}`}`;
}
type ServerStore = { _canvasNamespace: string };
async function encode(value: unknown): Promise<unknown> {
    if (value instanceof Blob) {
        const data = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result));
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(value);
        });
        return { __cineforge_blob: data };
    }
    if (Array.isArray(value)) return Promise.all(value.map(encode));
    if (value && typeof value === 'object') {
        return Object.fromEntries(await Promise.all(Object.entries(value).map(async ([key, child]) => [key, await encode(child)])));
    }
    return value;
}
async function decode(value: unknown): Promise<unknown> {
    if (Array.isArray(value)) return Promise.all(value.map(decode));
    if (value && typeof value === 'object') {
        const record = value as Record<string, unknown>;
        if (typeof record.__cineforge_blob === 'string' && record.__cineforge_blob.startsWith('data:')) {
            return (await fetch(record.__cineforge_blob)).blob();
        }
        return Object.fromEntries(await Promise.all(Object.entries(record).map(async ([key, child]) => [key, await decode(child)])));
    }
    return value;
}
const driver = {
    _driver: 'cineforge-server', _support: true,
    async _initStorage(this: ServerStore, options: { name: string; storeName: string }) {
        this._canvasNamespace = `${options.name}.${options.storeName}`;
    },
    async getItem(this: ServerStore, key: string) {
        const url = path(this._canvasNamespace, key);
        const response = await hostFetch(url);
        revisions.set(url, Number(response.headers.get('X-Canvas-Revision')));
        if (response.status === 204) return null;
        return response.headers.get('X-Canvas-Kind') === 'blob' ? response.blob() : decode(await response.json());
    },
    async setItem(this: ServerStore, key: string, value: unknown) {
        const url = path(this._canvasNamespace, key);
        const previous = pending.get(url) || Promise.resolve();
        const saving = previous.catch(() => {}).then(async () => {
            if (!revisions.has(url)) {
                const response = await hostFetch(url);
                revisions.set(url, Number(response.headers.get('X-Canvas-Revision')));
            }
            notify('saving');
            const blob = value instanceof Blob;
            const response = await hostApi<{ revision: number }>(`${url}?revision=${revisions.get(url) || 0}`, {
                method: 'PUT', headers: { 'X-Canvas-Kind': blob ? 'blob' : 'json',
                    'Content-Type': blob ? value.type || 'application/octet-stream' : 'application/json' },
                body: blob ? value : JSON.stringify(await encode(value ?? null)),
            });
            revisions.set(url, response.revision);
            notify('saved');
            return value;
        });
        pending.set(url, saving);
        try { return await saving; } finally { if (pending.get(url) === saving) pending.delete(url); }
    },
    async removeItem(this: ServerStore, key: string) {
        const url = path(this._canvasNamespace, key);
        await pending.get(url)?.catch(() => {});
        await hostFetch(url, { method: 'DELETE' });
        revisions.delete(url);
    },
    async keys(this: ServerStore) { return hostApi<string[]>(path(this._canvasNamespace)); },
    async length(this: ServerStore) { return (await driver.keys.call(this)).length; },
    async key(this: ServerStore, index: number) { return (await driver.keys.call(this))[index] ?? null; },
    async clear(this: ServerStore) {
        for (const key of await driver.keys.call(this)) await driver.removeItem.call(this, key);
    },
    async iterate(this: ServerStore, callback: (value: unknown, key: string, index: number) => unknown) {
        const keys = await driver.keys.call(this);
        for (let i = 0; i < keys.length; i++) {
            const result = callback(await driver.getItem.call(this, keys[i]), keys[i], i + 1);
            if (result !== undefined) return result;
        }
    },
};

export async function bootstrap() {
    const context = await hostApi<PlatformContext>('/canvas/config');
    window.cineforgeCanvas = context;
    await localforage.defineDriver(driver as unknown as Parameters<typeof localforage.defineDriver>[0]);
    const original = localforage.createInstance.bind(localforage);
    localforage.createInstance = options => original({ ...options, driver: 'cineforge-server' });
    localforage.config({ name: 'infinite-canvas', storeName: 'app_state', driver: 'cineforge-server' });
    await localforage.setDriver('cineforge-server');
    const key = `infinite-canvas:ai_config_store:${context.user_id}`;
    // Transfer this account's existing settings once, after a successful authenticated read.
    if (await localforage.getItem(key) === null) {
        const saved = localStorage.getItem(key);
        if (saved) {
            JSON.parse(saved);
            await localforage.setItem(key, saved);
            localStorage.removeItem(key);
        }
    } else {
        localStorage.removeItem(key);
    }
    const { useConfigStore } = await import('@/stores/use-config-store');
    const { usePromptSourceStore } = await import('@/stores/use-prompt-source-store');
    await Promise.all([useConfigStore.persist.rehydrate(), usePromptSourceStore.persist.rehydrate()]);
    if (!useConfigStore.persist.hasHydrated() || !usePromptSourceStore.persist.hasHydrated()) throw new Error('账号设置读取失败，请刷新重试');
    const config = { ...useConfigStore.getState().config };
    const models = context.models;
    const videoCaps = models.find(m => `cineforge::${m.id}` === config.videoModel)?.capabilities || {};
    const durations: number[] = videoCaps.duration_resolution_map?.flatMap((group: any) => group.durations) || videoCaps.durations || [];
    if (durations.length && !durations.includes(Number(config.videoSeconds))) config.videoSeconds = String(durations[0]);
    const resolutions: string[] = videoCaps.duration_resolution_map?.filter((group: any) => group.durations.includes(Number(config.videoSeconds))).flatMap((group: any) => group.resolutions) || videoCaps.resolutions || [];
    if (resolutions.length && !resolutions.includes(String(config.vquality).replace(/p$/, '') + 'p')) config.vquality = resolutions[0];
    if (videoCaps.audio_policy === 'disabled') config.videoGenerateAudio = 'false';
    if (videoCaps.audio_policy === 'required') config.videoGenerateAudio = 'true';
    await useConfigStore.setState({ config });
    notify('ready');
}
