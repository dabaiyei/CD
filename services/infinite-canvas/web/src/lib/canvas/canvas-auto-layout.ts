import { graphlib, layout } from '@dagrejs/dagre';
import type { CanvasConnection, CanvasNodeData, Position } from '@/types/canvas';

const GAP = 64; // Reserve room for node titles as well as the content rectangles.

// Connected components follow their dependency graph; unrelated components occupy compact rows.
function arrangeLevel(items: CanvasNodeData[], edges: Array<[string, string]>) {
    const graph = new graphlib.Graph().setDefaultEdgeLabel(() => ({}));
    items.forEach(node => graph.setNode(node.id, { width: node.width, height: node.height }));
    edges.forEach(([from, to]) => { if (from !== to) graph.setEdge(from, to); });
    const blocks = graphlib.alg.components(graph).map(ids => {
        const subgraph = new graphlib.Graph().setGraph({ rankdir: 'LR', ranksep: 120, nodesep: GAP }).setDefaultEdgeLabel(() => ({}));
        const members = new Set(ids);
        ids.forEach(id => subgraph.setNode(id, { ...graph.node(id) }));
        graph.edges().forEach(edge => { if (members.has(edge.v) && members.has(edge.w)) subgraph.setEdge(edge.v, edge.w); });
        layout(subgraph);
        const width = subgraph.graph().width!;
        const height = subgraph.graph().height!;
        const positions = ids.map(id => {
            const node = subgraph.node(id);
            return { id, x: node.x! - node.width / 2, y: node.y! - node.height / 2 };
        });
        return { width, height, positions };
    });
    const rowWidth = Math.max(0, ...blocks.map(block => block.width), Math.sqrt(blocks.reduce((area, block) => area + (block.width + GAP) * (block.height + GAP), 0) * 1.6));
    const positions = new Map<string, Position>();
    let x = 0, y = 0, rowHeight = 0, width = 0;
    for (const block of blocks) {
        if (x && x + block.width > rowWidth) { x = 0; y += rowHeight + GAP; rowHeight = 0; }
        block.positions.forEach(node => positions.set(node.id, { x: x + node.x, y: y + node.y }));
        width = Math.max(width, x + block.width);
        rowHeight = Math.max(rowHeight, block.height);
        x += block.width + GAP;
    }
    return { positions, width, height: y + rowHeight };
}

export function autoLayoutCanvas(nodes: CanvasNodeData[], connections: CanvasConnection[]): CanvasNodeData[] {
    if (!nodes.length) return nodes;
    const byId = new Map(nodes.map(node => [node.id, node]));
    const parents = new Map<string, string>();
    // Broken group references must not hide nodes or make layout recurse forever.
    for (const node of nodes) {
        const parent = node.metadata?.groupId;
        if (!parent || byId.get(parent)?.type !== 'group' || parent === node.id) continue;
        const seen = new Set([node.id]);
        let ancestor: string | undefined = parent;
        while (ancestor && !seen.has(ancestor)) { seen.add(ancestor); ancestor = byId.get(ancestor)?.metadata?.groupId; }
        if (!ancestor) parents.set(node.id, parent);
    }
    const children = new Map<string | undefined, CanvasNodeData[]>();
    for (const node of nodes) {
        const parent = parents.get(node.id);
        children.set(parent, [...(children.get(parent) || []), node]);
    }
    const result = new Map(byId);
    function arrange(parent?: string) {
        const items = children.get(parent) || [];
        for (const node of items) {
            if (children.has(node.id)) {
                const inner = arrange(node.id);
                // Match the existing group's 24px sides and 52px title area.
                result.set(node.id, { ...node, width: inner.width + 48, height: inner.height + 76 });
            }
        }
        const owners = new Map(items.map(node => [node.id, node.id]));
        function own(id: string, owner: string) {
            for (const child of children.get(id) || []) { owners.set(child.id, owner); own(child.id, owner); }
        }
        items.forEach(node => own(node.id, node.id));
        const edges: Array<[string, string]> = [];
        for (const edge of connections) {
            const from = owners.get(edge.fromNodeId), to = owners.get(edge.toNodeId);
            if (from && to && from !== to) edges.push([from, to]);
        }
        const arranged = arrangeLevel(items.map(node => result.get(node.id)!), edges);
        for (const [id, position] of arranged.positions) {
            result.set(id, { ...result.get(id)!, position: { x: position.x + (parent ? 24 : 0), y: position.y + (parent ? 52 : 0) } });
        }
        return arranged;
    }
    arrange();
    const absolute = new Map<string, Position>();
    function positionOf(id: string): Position {
        const cached = absolute.get(id);
        if (cached) return cached;
        const local = result.get(id)!.position;
        const parent = parents.get(id);
        const origin = parent ? positionOf(parent) : { x: 0, y: 0 };
        const position = { x: origin.x + local.x, y: origin.y + local.y };
        absolute.set(id, position);
        return position;
    }
    return nodes.map(node => ({ ...result.get(node.id)!, position: positionOf(node.id) }));
}
