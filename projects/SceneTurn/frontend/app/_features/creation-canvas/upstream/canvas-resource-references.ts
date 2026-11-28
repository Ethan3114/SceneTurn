import { imageReferenceLabel } from "./image-reference-prompt";
import i18n from "../i18n";
import { getNodeDefinition } from "./node-registry";
import { getDataUrlByteSize, readImageMeta } from "./image-utils";
import { imageToDataUrl } from "../media";
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from "./canvas";
import { canvasGeneratorOutputKind, isCanvasGenerationOutputReady } from './canvas-generation-outputs';

export type CanvasResourceKind = "image" | "video" | "audio" | "text";

export type CanvasResourceReference = {
    id: string;
    nodeId: string;
    kind: CanvasResourceKind;
    label: string;
    title: string;
    previewUrl?: string;
    text?: string;
    active: boolean;
};

export function buildNodeMentionReferences(node: CanvasNodeData, nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    return labelResourceNodes(getMentionResourceNodes(node.id, nodes, connections), true);
}

export function buildCanvasResourceReferences(nodes: CanvasNodeData[]) {
    return labelResourceNodes(nodes, true);
}

export async function resolveCanvasReferenceImages(references: CanvasResourceReference[], nodes: CanvasNodeData[]) {
    const nodesById = new Map(nodes.map((node) => [node.id, node]));
    return Promise.all(references.filter((reference) => reference.kind === "image").map(async (reference) => {
        const node = nodesById.get(reference.nodeId);
        if (!node) throw new Error(i18n.t("agent.composer.mentions.resourceMissing", { title: reference.title }));
        const metadata = node.metadata;
        const dataUrl = await imageToDataUrl({ storageKey: metadata?.storageKey, url: reference.previewUrl });
        if (!dataUrl.startsWith("data:image/")) throw new Error(i18n.t("agent.composer.mentions.imageReadFailed", { title: reference.title }));
        const meta = metadata?.naturalWidth && metadata.naturalHeight
            ? { width: metadata.naturalWidth, height: metadata.naturalHeight, mimeType: metadata.mimeType || dataUrl.match(/^data:([^;]+)/)?.[1] || "image/png" }
            : await readImageMeta(dataUrl);
        return {
            id: `canvas:${node.id}`,
            name: reference.title,
            type: metadata?.mimeType || meta.mimeType,
            size: metadata?.bytes || getDataUrlByteSize(dataUrl),
            width: meta.width,
            height: meta.height,
            url: reference.previewUrl || dataUrl,
            dataUrl,
        };
    }));
}

export function getMentionResourceNodes(nodeId: string, nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    const configInputs = expandGroupResourceNodes(getConnectedConfigInputNodes(nodeId, nodes, connections), nodes, connections);
    if (configInputs.length) return configInputs;
    const ownInputs = expandGroupResourceNodes(getContextInputNodes(nodeId, nodes, connections), nodes, connections);
    if (ownInputs.length) return ownInputs;
    const node = nodes.find((item) => item.id === nodeId);
    const resource = node && resolveCanvasGenerationOutput(node, nodes, connections);
    return resource && isResourceNode(resource) ? [resource] : [];
}

export function getGenerationResourceNodes(nodeId: string, nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    const configInputs = getConnectedConfigInputNodes(nodeId, nodes, connections);
    if (configInputs.length) return configInputs;
    const ownInputs = getContextInputNodes(nodeId, nodes, connections);
    if (ownInputs.length) return ownInputs;
    return [];
}

function getContextInputNodes(nodeId: string, nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    return connections
        .filter((connection) => connection.toNodeId === nodeId)
        .map((connection) => {
            const node = nodes.find((item) => item.id === connection.fromNodeId);
            return node && resolveCanvasGenerationOutput(node, nodes, connections);
        })
        .filter((node): node is CanvasNodeData => Boolean(node && isCanvasReferenceNode(node, nodes, connections)));
}

function getConnectedConfigInputNodes(nodeId: string, nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    const node = nodes.find((node) => node.id === nodeId);
    if (node && (node.type === CanvasNodeType.Config || canvasGeneratorOutputKind(node))) return [];
    const configConnection = connections.find((connection) => connection.fromNodeId === nodeId && nodes.find((node) => node.id === connection.toNodeId)?.type === CanvasNodeType.Config);
    if (!configConnection) return [];
    return getContextInputNodes(configConnection.toNodeId, nodes, connections).filter((node) => node.id !== nodeId);
}

export function resolveCanvasGenerationOutput(node: CanvasNodeData, nodes: CanvasNodeData[], connections: CanvasConnection[]): CanvasNodeData {
    const currentId = node.metadata?.outputNodeId;
    const legacyOutputKind = node.type === CanvasNodeType.Config && !node.metadata?.generationMode && currentId
        ? nodes.find((candidate) => candidate.id === currentId)?.type : undefined;
    const kind = canvasGeneratorOutputKind(node, legacyOutputKind);
    if (!kind) return node;
    const linkedIds = new Set(connections.filter((edge) => edge.fromNodeId === node.id).map((edge) => edge.toNodeId));
    let output: CanvasNodeData | undefined;
    for (const candidate of nodes) {
        if (candidate.id === node.id || candidate.type !== kind || !linkedIds.has(candidate.id) || !isCanvasGenerationOutputReady(candidate)) continue;
        const metadata = candidate.metadata;
        if (metadata?.publicMaterialRole || metadata?.creativeArtifact || (metadata?.inlineResultOf && metadata.inlineResultOf !== node.id)) continue;
        if (candidate.id === currentId) { output = candidate; break; }
        if (metadata?.inlineResultOf === node.id) output = candidate;
    }
    return output ? { ...output, id: node.id, title: node.title } : node;
}

function hasGroupResources(node: CanvasNodeData, nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    return node.type === CanvasNodeType.Group && getGroupResourceNodes(node.id, nodes, connections).length > 0;
}

export function isCanvasReferenceNode(node: CanvasNodeData, nodes: CanvasNodeData[], connections: CanvasConnection[] = []) {
    return isResourceNode(node) || hasGroupResources(node, nodes, connections);
}

function expandGroupResourceNodes(inputNodes: CanvasNodeData[], nodes: CanvasNodeData[], connections: CanvasConnection[]) {
    const resources = inputNodes.flatMap((node) => (node.type === CanvasNodeType.Group ? getGroupResourceNodes(node.id, nodes, connections) : [node]));
    return [...new Map(resources.map((node) => [node.id, node])).values()];
}

export function getGroupResourceNodes(groupId: string, nodes: CanvasNodeData[], connections: CanvasConnection[] = []) {
    return nodes.filter((node) => node.metadata?.groupId === groupId)
        .map((node) => resolveCanvasGenerationOutput(node, nodes, connections)).filter(isResourceNode);
}

function labelResourceNodes(nodes: CanvasNodeData[], active: boolean) {
    const counts: Record<CanvasResourceKind, number> = { image: 0, video: 0, audio: 0, text: 0 };
    return nodes.flatMap((node): CanvasResourceReference[] => {
        const kind = getCanvasResourceKind(node);
        if (!kind) return [];
        const resource = getNodeDefinition(node.type)?.resource?.(node);
        const index = counts[kind]++;
        const label = labelForKind(kind, index);
        return [
            {
                id: node.id,
                nodeId: node.id,
                kind,
                label,
                title: node.title || label,
                previewUrl: node.metadata?.content || resource?.url,
                text: resourceText(node),
                active,
            },
        ];
    });
}

function labelForKind(kind: CanvasResourceKind, index: number) {
    if (kind === "image") return imageReferenceLabel(index);
    if (kind === "video") return i18n.t("canvas.configNode.videoReferences") + ` ${index + 1}`;
    if (kind === "audio") return i18n.t("canvas.configNode.audioReferences") + ` ${index + 1}`;
    return i18n.t("canvas.composer.resources.text", { index: index + 1 });
}

function isResourceNode(node: CanvasNodeData) {
    return Boolean(getCanvasResourceKind(node));
}

function resourceText(node: CanvasNodeData): string | undefined {
    if (node.type === CanvasNodeType.Text) return node.metadata?.content || node.metadata?.prompt;
    const resource = getNodeDefinition(node.type)?.resource?.(node);
    return resource?.kind === "text" ? resource.text : undefined;
}

export function getCanvasResourceKind(node: CanvasNodeData): CanvasResourceKind | null {
    if (node.type === CanvasNodeType.Image && node.metadata?.content) return "image";
    if (node.type === CanvasNodeType.Video && node.metadata?.content) return "video";
    if (node.type === CanvasNodeType.Audio && node.metadata?.content) return "audio";
    if (node.type === CanvasNodeType.Text && (node.metadata?.content || node.metadata?.prompt)) return "text";
    // Plugin nodes declare their input eligibility through definition.resource.
    return getNodeDefinition(node.type)?.resource?.(node)?.kind || null;
}
