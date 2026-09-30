import { saveAs } from "file-saver";

import i18n from "@/i18n";
import { normalizeIntegratedConfig, useConfigStore, type AiConfig, type WebdavSyncConfig } from "@/stores/use-config-store";
import { normalizePromptSourceSettings, usePromptSourceStore, type PromptSourceSchedule } from "@/stores/use-prompt-source-store";
import type { PromptSource } from "@/services/api/prompt-source-presets";

type AppConfigFile = {
    app: "infinite-canvas";
    version: 1;
    exportedAt: string;
    config: AiConfig;
    webdav: WebdavSyncConfig;
    promptSources: {
        sources: PromptSource[];
        schedule: PromptSourceSchedule;
    };
};

export function exportAppConfig() {
    const { config, webdav } = useConfigStore.getState();
    const { sources, schedule } = usePromptSourceStore.getState();
    const data: AppConfigFile = { app: "infinite-canvas", version: 1, exportedAt: new Date().toISOString(), config, webdav, promptSources: { sources, schedule } };
    saveAs(new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" }), "infinite-canvas-config.json");
}

export async function importAppConfig(file: File) {
    let data: AppConfigFile;
    try {
        data = JSON.parse(await file.text()) as AppConfigFile;
    } catch {
        throw new Error(i18n.t("config.invalidFile"));
    }
    if (data.app !== "infinite-canvas" || data.version !== 1 || !data.config || !data.webdav || !data.promptSources) throw new Error(i18n.t("config.invalidFile"));
    if (!Array.isArray(data.config.channels) || data.config.channels.some(channel => !channel || !Array.isArray(channel.models))
        || !Array.isArray(data.promptSources.sources) || !data.promptSources.schedule) throw new Error(i18n.t("config.invalidFile"));
    const config = normalizeIntegratedConfig(data.config);
    const promptSources = normalizePromptSourceSettings(data.promptSources);
    await useConfigStore.setState({ config, webdav: data.webdav });
    await usePromptSourceStore.setState(promptSources);
}
