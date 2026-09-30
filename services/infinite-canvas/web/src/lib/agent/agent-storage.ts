// Standalone settings keep their original names; embedded connections belong to
// the authenticated account, including the restricted server bridge token.
export function agentStorageKey(name: string) {
    return window.cineforgeCanvas ? `${name}:${window.cineforgeCanvas.user_id}` : name;
}
