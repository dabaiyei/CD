export type ReferenceVideo = {
    id: string;
    name: string;
    type: string;
    url: string;
    storageKey?: string;
    bytes?: number;
    width?: number;
    height?: number;
    durationMs?: number;
    evidence?: import('./canvas').CanvasVideoEvidence;
};

export type ReferenceAudio = {
    id: string;
    name: string;
    type: string;
    url: string;
    storageKey?: string;
    durationMs?: number;
};
