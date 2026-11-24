import { nodeDramaTask } from '../drama-tasks';
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasNodeTypeId } from './canvas';

type OutputKind = CanvasNodeType.Text | CanvasNodeType.Image | CanvasNodeType.Video;
type GenerationReceipt = { sourceNodeId: string; outputNodeId?: string | null };
type InlineOutputs = { current: CanvasNodeData; versions: CanvasNodeData[] };

export function canvasGeneratorOutputKind(node: CanvasNodeData, legacyOutputKind?: CanvasNodeTypeId): OutputKind | undefined {
    const metadata = node.metadata;
    if (metadata?.productionTool || metadata?.publicMaterialRole || metadata?.creativeArtifact || metadata?.inlineResultOf) return;
    if (node.type === CanvasNodeType.Text && metadata?.generationMode === 'text') return CanvasNodeType.Text;
    if (node.type === CanvasNodeType.Video && !metadata?.content && !metadata?.storageKey && !metadata?.pendingResult) return CanvasNodeType.Video;
    if (node.type !== CanvasNodeType.Config || metadata?.dramaTask === 'join') return;
    switch (metadata?.generationMode ?? legacyOutputKind ?? nodeDramaTask(node).kind) {
        case 'text': return CanvasNodeType.Text;
        case 'image': return CanvasNodeType.Image;
        case 'video': return CanvasNodeType.Video;
    }
}

export function isCanvasGenerationOutputReady(node: CanvasNodeData) {
    return node.type === CanvasNodeType.Text ? Boolean(node.metadata?.content) : Boolean(node.metadata?.storageKey);
}

export function projectCanvasGenerationOutputs(
    nodes: CanvasNodeData[],
    connections: CanvasConnection[],
    generations: GenerationReceipt[],
) {
    const nodesById = new Map(nodes.map((node) => [node.id, node]));
    const kindsById = new Map(nodes.map((node) => [node.id, canvasGeneratorOutputKind(node, nodesById.get(node.metadata?.outputNodeId ?? '')?.type)]));
    const receiptsByOutputId = new Map<string, Set<string>>();
    for (const task of generations) {
        if (!task.outputNodeId) continue;
        const sources = receiptsByOutputId.get(task.outputNodeId) ?? new Set<string>();
        sources.add(task.sourceNodeId);
        receiptsByOutputId.set(task.outputNodeId, sources);
    }

    const ownershipByOutputId = new Map<string, { sourceId: string; evidence: number; ambiguous: boolean }>();
    for (const { fromNodeId, toNodeId } of connections) {
        const source = nodesById.get(fromNodeId);
        const output = nodesById.get(toNodeId);
        if (!source || !output || fromNodeId === toNodeId || kindsById.get(fromNodeId) !== output.type) continue;
        const outputMetadata = output.metadata;
        if (outputMetadata?.publicMaterialRole || outputMetadata?.creativeArtifact) continue;
        const markedSource = outputMetadata?.inlineResultOf;
        if (markedSource && markedSource !== source.id) continue;
        const evidence = markedSource ? 3 : receiptsByOutputId.get(output.id)?.has(source.id) ? 2 : source.metadata?.outputNodeId === output.id ? 1 : 0;
        if (evidence === 0) continue;
        const owner = ownershipByOutputId.get(output.id);
        if (!owner || evidence > owner.evidence) {
            ownershipByOutputId.set(output.id, { sourceId: source.id, evidence, ambiguous: false });
        } else if (evidence === owner.evidence && source.id !== owner.sourceId) {
            owner.ambiguous = true;
        }
    }

    const sourceIds = new Set([...ownershipByOutputId.values()].filter((owner) => !owner.ambiguous).map((owner) => owner.sourceId));
    const resultsBySourceId = new Map<string, CanvasNodeData[]>();
    const visibleNodeIds = new Map<string, string>();
    for (const output of nodes) {
        const owner = ownershipByOutputId.get(output.id);
        if (!owner || owner.ambiguous || sourceIds.has(output.id)) continue;
        const { sourceId } = owner;
        const versions = resultsBySourceId.get(sourceId) ?? [];
        versions.push(output);
        resultsBySourceId.set(sourceId, versions);
        visibleNodeIds.set(output.id, sourceId);
    }
    const outputsBySourceId = new Map<string, InlineOutputs>();
    for (const [sourceId, versions] of resultsBySourceId) {
        versions.reverse();
        const currentId = nodesById.get(sourceId)?.metadata?.outputNodeId;
        const current = versions.find((version) => version.id === currentId) ?? versions[0];
        outputsBySourceId.set(sourceId, { current, versions: [current, ...versions.filter((version) => version.id !== current.id)] });
    }

    const visibleConnections = new Map<string, CanvasConnection>();
    const connectionIdsByVisibleId = new Map<string, string[]>();
    for (const edge of connections) {
        const fromNodeId = visibleNodeIds.get(edge.fromNodeId) ?? edge.fromNodeId;
        const toNodeId = visibleNodeIds.get(edge.toNodeId) ?? edge.toNodeId;
        if (fromNodeId === toNodeId || !nodesById.has(fromNodeId) || !nodesById.has(toNodeId)) continue;
        const key = JSON.stringify([fromNodeId, toNodeId]);
        const existing = visibleConnections.get(key);
        if (existing) {
            connectionIdsByVisibleId.get(existing.id)?.push(edge.id);
        } else {
            visibleConnections.set(key, { ...edge, fromNodeId, toNodeId });
            connectionIdsByVisibleId.set(edge.id, [edge.id]);
        }
    }
    return {
        nodes: nodes.filter((node) => !visibleNodeIds.has(node.id)),
        connections: [...visibleConnections.values()],
        outputsBySourceId,
        visibleNodeIds,
        connectionIdsByVisibleId,
    };
}
