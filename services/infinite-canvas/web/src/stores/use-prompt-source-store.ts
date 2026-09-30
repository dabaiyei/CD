import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { localForageStorage } from "@/lib/localforage-storage";

import { DEFAULT_PROMPT_SOURCES, createPromptSource, type PromptSource } from "@/services/api/prompt-source-presets";

export type PromptSourceSchedule = {
    intervalMinutes: number;
    lastFetchedAt: string;
};

const PROMPT_SOURCE_STORE_KEY = `infinite-canvas:prompt_source_store_v2${window.cineforgeCanvas ? `:${window.cineforgeCanvas.user_id}` : ''}`;

const defaultSchedule: PromptSourceSchedule = {
    intervalMinutes: 30,
    lastFetchedAt: "",
};

export const PROMPT_SOURCE_INTERVALS = [0, 30, 60, 360, 1440];

type PromptSourceStore = {
    sources: PromptSource[];
    schedule: PromptSourceSchedule;
    addSource: () => PromptSource;
    saveSource: (source: PromptSource) => void;
    removeSource: (id: string) => void;
    toggleSource: (id: string, enabled: boolean) => void;
    updateSchedule: <K extends keyof PromptSourceSchedule>(key: K, value: PromptSourceSchedule[K]) => void;
};

export function normalizePromptSourceSettings(input: Partial<Pick<PromptSourceStore, 'sources' | 'schedule'>>) {
    const savedSources = Array.isArray(input.sources) ? input.sources : [];
    const enabledById = new Map(savedSources.map(source => [source.id, source.enabled]));
    const builtIn = DEFAULT_PROMPT_SOURCES.map(source => ({ ...source, enabled: enabledById.get(source.id) ?? source.enabled }));
    const builtInIds = new Set(builtIn.map(source => source.id));
    const custom = savedSources.filter(source => !source.builtIn && !builtInIds.has(source.id)).map(source => createPromptSource(source));
    return { sources: [...builtIn, ...custom], schedule: { ...defaultSchedule, ...(input.schedule || {}) } };
}

export const usePromptSourceStore = create<PromptSourceStore>()(
    persist(
        (set) => ({
            sources: DEFAULT_PROMPT_SOURCES,
            schedule: defaultSchedule,
            addSource: () => createPromptSource(),
            saveSource: (source) =>
                set((state) => ({
                    sources: state.sources.some((item) => item.id === source.id)
                        ? state.sources.map((item) => (item.id === source.id && !item.builtIn ? createPromptSource(source) : item))
                        : [...state.sources, createPromptSource(source)],
                })),
            removeSource: (id) => set((state) => ({ sources: state.sources.filter((item) => item.id !== id || item.builtIn) })),
            toggleSource: (id, enabled) => set((state) => ({ sources: state.sources.map((item) => (item.id === id ? { ...item, enabled } : item)) })),
            updateSchedule: (key, value) => set((state) => ({ schedule: { ...state.schedule, [key]: value } })),
        }),
        {
            name: PROMPT_SOURCE_STORE_KEY,
            storage: createJSONStorage(() => window.cineforgeCanvas ? localForageStorage : localStorage),
            skipHydration: Boolean(window.cineforgeCanvas),
            partialize: (state) => ({ sources: state.sources, schedule: state.schedule }),
            merge: (persisted, current) => {
                const persistedState = (persisted || {}) as Partial<PromptSourceStore>;
                return { ...current, ...normalizePromptSourceSettings(persistedState) };
            },
        },
    ),
);
