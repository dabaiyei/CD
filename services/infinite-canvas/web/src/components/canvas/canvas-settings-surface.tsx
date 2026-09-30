import type { CSSProperties, ReactNode, RefObject } from 'react';
import { ConfigProvider } from 'antd';

/** Scope menus and outside clicks to the panel, without clipping menus inside its scroll area. */
export function CanvasSettingsSurface({ panelRef, style, children }: {
    panelRef: RefObject<HTMLDivElement | null>;
    style: CSSProperties;
    children: ReactNode;
}) {
    const availableHeight = window.innerHeight - 24;
    const edge = typeof style.bottom === 'number' ? 'bottom' : 'top';
    const offset = Math.max(12, Math.min(Number(style[edge]) || 12, window.innerHeight - Math.min(260, availableHeight) - 12));
    const maxHeight = Math.min(typeof style.maxHeight === 'number' ? style.maxHeight : availableHeight, window.innerHeight - offset - 12);
    return <ConfigProvider getPopupContainer={trigger => trigger?.closest<HTMLElement>('.canvas-settings-surface') || document.body}>
        <div ref={panelRef} className="canvas-settings-surface canvas-image-settings-popover"
            style={{ ...style, [edge]: offset, padding: 0, maxHeight: undefined, overflow: 'visible', overflowY: 'visible' }}
            onPointerDown={event => event.stopPropagation()}
            onMouseDown={event => event.stopPropagation()}
            onClick={event => event.stopPropagation()}>
            <div className="canvas-settings-scroll" style={{ padding: style.padding, maxHeight,
                overflowY: 'auto', borderRadius: style.borderRadius }}>
                {children}
            </div>
        </div>
    </ConfigProvider>;
}
