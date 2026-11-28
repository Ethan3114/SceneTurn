import { describe, expect, it } from 'vitest';

import { CanvasNodeType, type CanvasNodeData, type CanvasNodeMetadata } from '../../app/_features/creation-canvas/upstream/canvas';
import { canvasGeneratorOutputKind, isCanvasGenerationOutputReady, projectCanvasGenerationOutputs } from '../../app/_features/creation-canvas/upstream/canvas-generation-outputs';
import { normalizeConnection } from '../../app/_features/creation-canvas/upstream/canvas-node-geometry';
import { buildNodeGenerationInputs } from '../../app/_features/creation-canvas/upstream/canvas-node-generation';
import { resolveCanvasGenerationOutput } from '../../app/_features/creation-canvas/upstream/canvas-resource-references';

function node(id: string, type: CanvasNodeType, metadata: CanvasNodeMetadata = {}): CanvasNodeData {
    return { id, type, title: id, width: 350, height: 200, position: { x: 10, y: 20 }, metadata };
}

const link = (fromNodeId: string, toNodeId: string, id = `${fromNodeId}:${toNodeId}`) => ({ id, fromNodeId, toNodeId });

describe('素材生成卡片', () => {
    it.each([CanvasNodeType.Text, CanvasNodeType.Image, CanvasNodeType.Video] as const)('保留%s卡片的原位置和对外连线', (kind) => {
        const source = node('generator', kind === CanvasNodeType.Text ? kind : CanvasNodeType.Config, { generationMode: kind, outputNodeId: 'result' });
        const content = kind === CanvasNodeType.Text ? { content: '列车到站。' } : { storageKey: 'asset', content: '/api/v1/assets/asset/content' };
        const result = { ...node('result', kind, content), position: { x: 500, y: 20 } };
        const consumer = node('consumer', CanvasNodeType.Config, { generationMode: 'video' });
        const nodes = [source, result, consumer];
        const edges = [link(source.id, result.id), link(result.id, consumer.id, 'original'), link(source.id, consumer.id, 'from-card')];
        const projected = projectCanvasGenerationOutputs(nodes, edges, []);

        expect(projected.nodes).toEqual([source, consumer]);
        expect(projected.outputsBySourceId.get(source.id)).toEqual({ current: result, versions: [result] });
        expect(projected.visibleNodeIds.get(result.id)).toBe(source.id);
        expect(projected.connections).toEqual([link(source.id, consumer.id, 'original')]);
        expect(projected.connectionIdsByVisibleId.get('original')).toEqual(['original', 'from-card']);
        expect(nodes).toEqual([source, result, consumer]);
        expect(edges[1]).toEqual(link(result.id, consumer.id, 'original'));
        expect(normalizeConnection(source.id, consumer.id, nodes, 'source')).toEqual({ fromNodeId: source.id, toNodeId: consumer.id });
        expect(resolveCanvasGenerationOutput(source, nodes, edges)).toMatchObject({ id: source.id, type: kind, metadata: content });
    });

    it('保留当前任务、可预览的历史结果和独立参考素材', () => {
        const source = node('generator', CanvasNodeType.Config, { generationMode: 'image', outputNodeId: 'pending' });
        const history = node('history', CanvasNodeType.Image, { storageKey: 'earlier' });
        const latest = node('latest', CanvasNodeType.Image, { storageKey: 'newer', inlineResultOf: source.id });
        const reference = node('reference', CanvasNodeType.Image, { storageKey: 'imported' });
        const pending = node('pending', CanvasNodeType.Image, { pendingResult: true });
        const nodes = [source, history, latest, reference, pending];
        const edges = nodes.slice(1).map((target) => link(source.id, target.id));
        const projected = projectCanvasGenerationOutputs(nodes, edges, [{ sourceNodeId: source.id, outputNodeId: history.id }]);

        expect(projected.nodes).toEqual([source, reference]);
        expect(projected.outputsBySourceId.get(source.id)).toEqual({ current: pending, versions: [pending, latest, history] });
        expect(isCanvasGenerationOutputReady(pending)).toBe(false);
        expect(resolveCanvasGenerationOutput(source, nodes, edges)).toMatchObject({ id: source.id, metadata: { storageKey: 'newer' } });
    });

    it('仅将同类型且明确关联的生成结果放入卡片', () => {
        const source = node('source', CanvasNodeType.Config, { generationMode: 'image', outputNodeId: 'shared' });
        const other = node('other', CanvasNodeType.Config, { generationMode: 'image', outputNodeId: 'shared' });
        const shared = node('shared', CanvasNodeType.Image, { storageKey: 'kept' });
        const external = node('external', CanvasNodeType.Image, { publicMaterialRole: 'scene' });
        const unlinked = node('unlinked', CanvasNodeType.Image, { inlineResultOf: source.id });
        const wrongType = node('wrong-type', CanvasNodeType.Text, { inlineResultOf: source.id });
        const nodes = [source, other, shared, external, unlinked, wrongType];
        const edges = [link(source.id, shared.id), link(other.id, shared.id), link(source.id, external.id), link(source.id, wrongType.id)];

        expect(projectCanvasGenerationOutputs(nodes, edges, []).nodes).toEqual(nodes);
        expect(projectCanvasGenerationOutputs(nodes, edges, [{ sourceNodeId: source.id, outputNodeId: shared.id }]).visibleNodeIds.get(shared.id)).toBe(source.id);
        expect(canvasGeneratorOutputKind(node('join', CanvasNodeType.Config, { generationMode: 'video', dramaTask: 'join' }))).toBeUndefined();
        expect(canvasGeneratorOutputKind(node('director', CanvasNodeType.Config, { productionTool: 'director' }))).toBeUndefined();
    });

    it('下游生成可以直接读取组内卡片的文字结果', () => {
        const group = node('group', CanvasNodeType.Group);
        const text = node('text', CanvasNodeType.Text, { generationMode: 'text', groupId: group.id, outputNodeId: 'waiting', content: '描述一个镜头。' });
        const ready = node('ready', CanvasNodeType.Text, { inlineResultOf: text.id, content: '列车驶过泛着金光的站台。' });
        const waiting = node('waiting', CanvasNodeType.Text, { inlineResultOf: text.id, pendingResult: true });
        const image = node('image', CanvasNodeType.Config, { generationMode: 'image' });
        const nodes = [group, text, ready, waiting, image];
        const edges = [link(text.id, ready.id), link(text.id, waiting.id), link(group.id, image.id)];

        expect(buildNodeGenerationInputs(image.id, nodes, edges)).toEqual([
            { nodeId: group.id, title: group.title, type: 'group', children: [{ nodeId: text.id, title: text.title, type: 'text', text: ready.metadata?.content }] },
        ]);
    });

    it('使用旧版配置中已连接的主结果识别生成类型', () => {
        const source = node('旧版配置', CanvasNodeType.Config, { outputNodeId: 'result' });
        const output = node('result', CanvasNodeType.Text, { content: '站台上点起了一排灯。' });
        const nodes = [source, output];
        const edges = [link(source.id, output.id)];
        expect(projectCanvasGenerationOutputs(nodes, edges, []).outputsBySourceId.get(source.id)?.current).toEqual(output);
        expect(resolveCanvasGenerationOutput(source, nodes, edges)).toMatchObject({ id: source.id, type: CanvasNodeType.Text, metadata: output.metadata });
    });
});
