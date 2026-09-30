export type IndexedDbStoreUsage = { name: string; records: number; bytes: number };
export type IndexedDbDatabaseUsage = { name: string; version: number; bytes: number; stores: IndexedDbStoreUsage[] };
export type LocalStorageUsage = { usage: number | null; quota: number | null; contentBytes: number; databases: IndexedDbDatabaseUsage[] };

export async function readLocalStorageUsage(): Promise<LocalStorageUsage> {
    const [estimate, database] = await Promise.all([
        navigator.storage?.estimate ? navigator.storage.estimate().catch(() => null) : null,
        readDatabaseUsage("infinite-canvas"),
    ]);
    const { usage, quota } = estimate || {};
    return { usage: typeof usage === "number" && Number.isFinite(usage) ? usage : null,
        quota: typeof quota === "number" && Number.isFinite(quota) && quota > 0 ? quota : null,
        contentBytes: database?.bytes ?? 0, databases: database ? [database] : [] };
}

function readDatabaseUsage(name: string) {
    return new Promise<IndexedDbDatabaseUsage | null>((resolve, reject) => {
        const request = indexedDB.open(name);
        let missing = false;
        // Statistics must not create an empty database when data is stored on the server.
        request.onupgradeneeded = () => {
            missing = true;
            request.transaction?.abort();
        };
        request.onerror = () => missing ? resolve(null) : reject(request.error);
        request.onsuccess = () => {
            const database = request.result;
            const names = Array.from(database.objectStoreNames);
            if (!names.length) {
                database.close();
                resolve({ name, version: database.version, bytes: 0, stores: [] });
                return;
            }
            const transaction = database.transaction(names, "readonly");
            Promise.all(names.map((storeName) => readStoreUsage(transaction.objectStore(storeName))))
                .then((stores) => resolve({ name, version: database.version, bytes: stores.reduce((total, store) => total + store.bytes, 0), stores: stores.sort((a, b) => b.bytes - a.bytes) }))
                .catch(reject)
                .finally(() => database.close());
        };
    });
}

function readStoreUsage(store: IDBObjectStore) {
    return new Promise<IndexedDbStoreUsage>((resolve, reject) => {
        let records = 0;
        let bytes = 0;
        const request = store.openCursor();
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
            const cursor = request.result;
            if (!cursor) {
                resolve({ name: store.name, records, bytes });
                return;
            }
            records += 1;
            bytes += valueBytes(cursor.value);
            cursor.continue();
        };
    });
}

function valueBytes(value: unknown) {
    if (value instanceof Blob) return value.size;
    if (value instanceof ArrayBuffer) return value.byteLength;
    if (ArrayBuffer.isView(value)) return value.byteLength;
    return new TextEncoder().encode(typeof value === "string" ? value : JSON.stringify(value)).byteLength;
}
