export type PlatformModel = { id: string; name: string; type: string; is_default: boolean; capabilities: Record<string, any> };
export type PlatformContext = { user_id: string; is_admin?: boolean; models: PlatformModel[] };
declare global { interface Window { cineforgeCanvas?: PlatformContext } }

let refresh: Promise<void> | null = null;
export function notify(status: string, message?: string) {
    window.parent.postMessage({ source: 'cineforge-canvas', status, message }, window.location.origin);
}
export async function hostFetch(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
    const headers = new Headers(init.headers);
    const token = localStorage.getItem('cineforge.access-token');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: 'same-origin' });
    if (response.status === 401 && retry) {
        refresh ??= fetch('/api/v1/auth/refresh', { method: 'POST', credentials: 'same-origin' }).then(async r => {
            if (!r.ok) throw new Error('登录已过期，请重新登录');
            localStorage.setItem('cineforge.access-token', (await r.json()).access_token);
        }).finally(() => { refresh = null });
        await refresh;
        return hostFetch(path, init, false);
    }
    if (!response.ok) {
        let message = `请求失败（${response.status}）`;
        try { message = (await response.json()).detail || message; } catch { /* proxy error */ }
        notify('error', String(message));
        throw new Error(String(message));
    }
    return response;
}
export async function hostApi<T>(path: string, init: RequestInit = {}): Promise<T> {
    return (await hostFetch(path, init)).json();
}
