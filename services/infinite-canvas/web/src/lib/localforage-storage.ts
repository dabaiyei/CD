import localforage from "localforage";
import type { StateStorage } from "zustand/middleware";

localforage.config({
    name: "infinite-canvas",
    storeName: "app_state",
});

export const localForageStorage: StateStorage = {
    getItem: async (name) => {
        if (typeof window === "undefined") return null;
        try {
            return (await localforage.getItem<string>(name)) || null;
        } catch {
            if (window.cineforgeCanvas) throw new Error('画布读取失败，请刷新重试');
            return window.localStorage.getItem(name);
        }
    },
    setItem: async (name, value) => {
        if (typeof window === "undefined") return;
        try {
            await localforage.setItem(name, value);
        } catch {
            if (window.cineforgeCanvas) throw new Error('画布保存失败，修改尚未保存，请重试');
            window.localStorage.setItem(name, value);
        }
    },
    removeItem: async (name) => {
        if (typeof window === "undefined") return;
        try {
            await localforage.removeItem(name);
        } catch {
            if (window.cineforgeCanvas) throw new Error('画布删除失败，请重试');
            window.localStorage.removeItem(name);
        }
    },
};
