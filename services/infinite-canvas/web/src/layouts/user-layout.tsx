import type { ReactNode } from "react";
import { PlatformToolbar } from '@/cineforge/toolbar';

import { AgentPanel } from "@/components/agent/agent-panel";
import { AppTopNav } from "@/components/layout/app-top-nav";

export default function UserLayout({ children }: { children: ReactNode }) {
    return (
        <div className="flex h-dvh overflow-hidden bg-background text-foreground">
            <div className="platform-canvas-main flex min-w-0 flex-1 flex-col overflow-hidden">
                {window.cineforgeCanvas ? <PlatformToolbar /> : <AppTopNav />}
                <div className="min-h-0 flex-1 overflow-hidden">{children}</div>
            </div>
            <AgentPanel />
        </div>
    );
}
