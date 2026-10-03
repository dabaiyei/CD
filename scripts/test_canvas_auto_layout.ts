import assert from 'node:assert/strict';
import test from 'node:test';
import { autoLayoutCanvas } from '../services/infinite-canvas/web/src/lib/canvas/canvas-auto-layout.ts';
import type { CanvasNodeData, CanvasConnection } from '../services/infinite-canvas/web/src/types/canvas.ts';

const node = (id: string, width = 240, height = 160, groupId?: string): CanvasNodeData => ({
    id, type: 'image', title: id, width, height, position: { x: 10, y: 10 },
    metadata: { groupId, content: `image:${id}`, prompt: `保留${id}的提示词` },
});
const edge = (from: string, to: string): CanvasConnection => ({ id: `${from}-${to}`, fromNodeId: from, toNodeId: to });
function noOverlap(nodes: CanvasNodeData[]) {
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        assert(a.position.x + a.width <= b.position.x || b.position.x + b.width <= a.position.x ||
            a.position.y + a.height <= b.position.y || b.position.y + b.height <= a.position.y, `${a.id} overlaps ${b.id}`);
    }
}

test('branched flows use actual sizes without overlap or loss of content', () => {
    const nodes = [node('source', 480, 270), node('config', 300, 200), node('text', 560, 780), node('out', 320, 180)];
    const connections = [edge('source', 'config'), edge('source', 'text'), edge('config', 'out'), edge('text', 'out')];
    const before = structuredClone(nodes), links = structuredClone(connections);
    const result = autoLayoutCanvas(nodes, connections);
    noOverlap(result);
    for (const link of connections) {
        const from = result.find(n => n.id === link.fromNodeId)!, to = result.find(n => n.id === link.toNodeId)!;
        assert(from.position.x + from.width < to.position.x);
    }
    assert.deepEqual(nodes, before);
    assert.deepEqual(connections, links);
    result.forEach((n, i) => assert.deepEqual({ ...n, position: nodes[i].position }, nodes[i]));
    assert.deepEqual(autoLayoutCanvas(result, connections), result, 'arranging twice should be stable');
});

test('67 disconnected media occupy compact rows rather than overlapping or forming one tall column', () => {
    const nodes = Array.from({ length: 67 }, (_, i) => node(`media-${i}`, 160 + i % 3 * 90, 120 + i % 4 * 60));
    const result = autoLayoutCanvas(nodes, []);
    noOverlap(result);
    assert(new Set(result.map(n => n.position.x)).size > 1);
    assert(new Set(result.map(n => n.position.y)).size > 1);
    assert.deepEqual(result.map(n => n.metadata), nodes.map(n => n.metadata));
});

test('nested groups wrap their members and participate in external reference flow', () => {
    const outer = { ...node('outer'), type: 'group' }, inner = { ...node('inner', 200, 100, 'outer'), type: 'group' };
    const nodes = [outer, inner, node('frame1', 360, 240, 'inner'), node('frame2', 220, 300, 'inner'), node('text', 600, 720, 'outer'), node('config')];
    const result = autoLayoutCanvas(nodes, [edge('frame1', 'frame2'), edge('frame2', 'config'), edge('text', 'config')]);
    const byId = new Map(result.map(n => [n.id, n]));
    for (const item of result) {
        if (!item.metadata?.groupId) continue;
        const parent = byId.get(item.metadata.groupId)!;
        assert(item.position.x >= parent.position.x + 24 && item.position.y >= parent.position.y + 52);
        assert(item.position.x + item.width <= parent.position.x + parent.width - 24);
        assert(item.position.y + item.height <= parent.position.y + parent.height - 24);
    }
    noOverlap(result.filter(n => !n.metadata?.groupId));
    noOverlap(result.filter(n => n.metadata?.groupId === 'outer'));
    noOverlap(result.filter(n => n.metadata?.groupId === 'inner'));
    assert(byId.get('outer')!.position.x + byId.get('outer')!.width < byId.get('config')!.position.x);
    assert.deepEqual(result.map(n => n.metadata), nodes.map(n => n.metadata));
});

test('cycles, self links, missing references and broken groups remain finite and retain nodes', () => {
    const nodes = [node('a'), node('b'), node('orphan', 240, 160, 'missing'),
        { ...node('g1', 240, 160, 'g2'), type: 'group' }, { ...node('g2', 240, 160, 'g1'), type: 'group' }];
    const result = autoLayoutCanvas(nodes, [edge('a', 'b'), edge('b', 'a'), edge('a', 'a'), edge('missing', 'a')]);
    assert.equal(result.length, nodes.length);
    noOverlap(result);
    result.forEach(n => assert(Number.isFinite(n.position.x) && Number.isFinite(n.position.y)));
    assert.deepEqual(autoLayoutCanvas([], []), []);
    assert.deepEqual(autoLayoutCanvas([node('single')], []).map(n => n.metadata), [node('single').metadata]);
});

test('dense references and multiple groups remain separated without modifying dimensions of media', () => {
    const nodes = Array.from({ length: 100 }, (_, i) => node(`n${i}`, 120 + i % 5 * 20, 100 + i % 4 * 40));
    const connections = nodes.slice(1).flatMap((n, i) => [edge(nodes[Math.floor(i / 3)].id, n.id), edge('n0', n.id)]);
    const result = autoLayoutCanvas(nodes, connections);
    noOverlap(result);
    result.forEach((n, i) => assert.deepEqual([n.width, n.height, n.metadata], [nodes[i].width, nodes[i].height, nodes[i].metadata]));
});
