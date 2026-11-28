'use client';

import { addAssetToSceneTimeline, saveCloudProject } from '@/app/_utils/cloudApi';
import { CanvasBrowser, AssetThumbnail } from '@/app/_features/creation-canvas/CanvasBrowser';
import { CanvasShortcutTable, CanvasToolDirectory, CharacterStudio, emptyCharacterForm, type CharacterForm } from '@/app/_features/creation-canvas/CanvasWorkspaceTools';
import { CanvasVersionHistory } from '@/app/_features/creation-canvas/CanvasVersionHistory';
import { useMaterialWorkspace } from '@/app/_features/material-workspace/store';
import MaterialChat from '@/app/_features/material-workspace/MaterialChat';
import { getCanvasChatReferences } from '@/app/_utils/materialApi';
import { Boxes, CircleHelp, Keyboard, Layers, MessageSquare, Shapes, UserRound } from 'lucide-react';
import '@/app/_features/creation-canvas/embedded-materials.css';

import React, { useCallback, useEffect, useEffectEvent, useImperativeHandle, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import dynamic from 'next/dynamic';
import { AudioComposer } from '@/app/_features/creation-canvas/production/AudioComposer';
import { MediaCutEditor } from '@/app/_features/creation-canvas/production/MediaCutEditor';
import { Modal } from 'antd';
import { z } from 'zod';
import { AudioLines, ChevronLeft, ChevronRight, Copy, Film, Focus, FolderOpen, Grid2x2, Group, Hand, History, ImageIcon, Map as MapIcon, Network, NotebookText, PanelRight, Plus, Save, Scissors, Search, Trash2, Type, Ungroup, Upload } from 'lucide-react';

import { CanvasProviders } from '@/app/_features/creation-canvas/CanvasProviders';
import { CanvasImageTaskCard, canvasImageStatus } from '@/app/_features/creation-canvas/CanvasImageTaskCard';
import { canvasImageSize } from '@/app/_features/creation-canvas/canvas-image-settings';
import { canvasVideoRatio, canvasVideoReferenceImageIds, canvasVideoModeSupported, type CanvasVideoSourceMode } from '@/app/_features/creation-canvas/canvas-video-settings';
import { GenerationTaskCard, VideoGenerationReview, taskLabels } from '@/app/_features/creation-canvas/GenerationTaskCard';
import { TextGenerationTaskCard, textGenerationStatus } from '@/app/_features/creation-canvas/TextGenerationTaskCard';
import { findDramaTask, nodeDramaTask } from '@/app/_features/creation-canvas/drama-tasks';
import { buildShotFrameGraph } from '@/app/_features/creation-canvas/plot-shots';
import { upstreamBranchEdges } from '@/app/_features/creation-canvas/agent-branches';
import { canMergeCanvasEdits, mergeCanvasSnapshots, sameValue } from '@/app/_features/creation-canvas/merge-canvas';
import { CanvasNodeSplitDialog } from '@/app/_features/creation-canvas/CanvasNodeSplitDialog';
import { CanvasResultTools, EditResult } from '@/app/_features/creation-canvas/CanvasResultTools';
import { CanvasScriptTable, createCanvasScriptRow, type CanvasScriptRow } from '@/app/_features/creation-canvas/CanvasScriptTable';
import { characterExample, formatShot, linkNodes, placeScriptNodes, prepareShotNodes, reverseExample, scriptExample } from '@/app/_features/creation-canvas/canvas-script-actions';
import type { TextShortcut } from '@/app/_features/creation-canvas/CanvasTextShortcuts';
import { CanvasTextGenerator } from '@/app/_features/creation-canvas/CanvasTextGenerator';
import { InfiniteCanvas } from '@/app/_features/creation-canvas/upstream/infinite-canvas';
import { CanvasNode } from '@/app/_features/creation-canvas/upstream/canvas-node';
import { CanvasConfigNodePanel } from '@/app/_features/creation-canvas/upstream/canvas-config-node-panel';
import { ActiveConnectionPath, ConnectionPath } from '@/app/_features/creation-canvas/upstream/canvas-connections';
import { ConnectionCreateMenu, NodeCreateMenu, type CanvasCreateKind, type LinkedGenerationOption, type PendingConnectionCreate } from '@/app/_features/creation-canvas/upstream/canvas-create-menus';
import { Minimap } from '@/app/_features/creation-canvas/upstream/canvas-mini-map';
import { registerBuiltinNodes } from '@/app/_features/creation-canvas/upstream/builtin-nodes';
import { getNodeSpec } from '@/app/_features/creation-canvas/upstream/canvas-constants';
import { applyGroupSelection, applyUngroupSelection, canGroupSelectedNodes, canUngroupSelectedNodes, collectGroupMemberNodes, focusViewportForNode, getGroupWrapRect, nodeBounds, normalizeConnection } from '@/app/_features/creation-canvas/upstream/canvas-node-geometry';
import { buildNodeGenerationContext, buildNodeGenerationInputs, type NodeGenerationInput } from '@/app/_features/creation-canvas/upstream/canvas-node-generation';
import { buildNodeMentionReferences, getCanvasResourceKind, getGroupResourceNodes, isCanvasReferenceNode, resolveCanvasGenerationOutput, type CanvasResourceKind } from '@/app/_features/creation-canvas/upstream/canvas-resource-references';
import { canvasGeneratorOutputKind, projectCanvasGenerationOutputs } from '@/app/_features/creation-canvas/upstream/canvas-generation-outputs';
import { canvasThemes } from '@/app/_features/creation-canvas/upstream/canvas-theme';
import { useThemeStore } from '@/app/_features/creation-canvas/upstream/use-theme-store';
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasNodeMetadata, type ConnectionHandle, type Position } from '@/app/_features/creation-canvas/upstream/canvas';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { useTaskPolling } from '@/app/_hooks/useTaskPolling';
import { ApiError, acceptProjectEnvelope, adoptGeneration, applyTextGeneration, assetContentUrl, confirmGeneration, createCloudScene, createTextGeneration, getCloudCanvas, getCloudProject, getGenerations, getTextGeneration, getTextGenerations, getTextSettings, getWanSettings, joinGeneration, operationId, quoteGeneration, resumeGeneration, saveCloudCanvas, splitCloudImage, stopTextGeneration, uploadCloudAsset, type CanvasSnapshot, type GenerationView, type ImageSplitParams, type ProjectEnvelope, type TextGenerationView, type TextSettings } from '@/app/_utils/cloudApi';
import { WorkspaceShell, useWorkspaceUser, errorText } from './WorkspaceShell';
import AgentCanvasNode from './AgentCanvasNode';
import { getProviderCatalog, type ModelProfile, type ProviderCatalog } from '@/app/_utils/providerApi';
import { confirmCanvasImageJob, createCanvasImageJob, getCanvasImageJobs, type CanvasImageJob } from '@/app/_utils/canvasImageApi';
import type { SceneTurnAsset } from '@/app/_types';
import '@/app/_features/creation-canvas/libtv-workspace.css';

registerBuiltinNodes();
const DirectorEditor = dynamic(() => import('@/app/_features/creation-canvas/production/DirectorEditor'), { ssr: false });
const emptyCanvas = (sceneId: string): CanvasSnapshot => ({ sceneId, nodes: [], connections: [], viewport: { x: 40, y: 100, k: 1 } });
const rawPrompt = (node: CanvasNodeData) => node.metadata?.composerContent ?? node.metadata?.prompt ?? (node.type === CanvasNodeType.Text ? node.metadata?.content : '') ?? '';
function makeNode(type: string, position: Position): CanvasNodeData {
  if (type === 'agent') return { id: operationId(), type, title: '素材创作助手', position, width: 460, height: 720, metadata: { agentWorkspace: 'scene', agentRole: 'writer', agentGoal: '', agentExecutor: 'platform', agentTargetIds: [] } };
  const spec = getNodeSpec(type);
  return { id: operationId(), type, title: type === CanvasNodeType.Config ? '生成配置' : spec.title, position, width: spec.width, height: spec.height, metadata: { ...spec.metadata, ...(type === CanvasNodeType.Config ? { generationMode: 'video', dramaTask: 'clip' } : {}) } };
}
const createTasks = {
  image: { task: 'shotImage', kind: 'image', title: '图片节点', width: 350, height: 200 },
  video: { task: 'clip', kind: 'video', title: '视频节点', width: 350, height: 200 },
  script: { task: 'plot', kind: 'text', title: '脚本生成器', width: 350, height: 350 },
  'smart-edit': { task: 'join', kind: 'video', title: '智能剪辑', width: 350, height: 350 },
} as const;

function nodeForMenu(kind: CanvasCreateKind, position: Position, legacyScript = false): CanvasNodeData {
  if (kind === 'director') return { ...makeNode(CanvasNodeType.Config, position), title:'导演台', width:350, height:350, metadata:{productionTool:'director'} };
  if (kind === 'text' || kind === 'audio') {
    const node = makeNode(kind, position);
    return { ...node, title: kind === 'text' ? '文本节点' : '音频节点', width: kind === 'text' ? 350 : 320, height: 350,
      metadata: { ...node.metadata, ...(kind === 'text' ? { generationMode: 'text', dramaTask: 'text' } : {}) } };
  }
  const { task, kind: generationMode, title, width, height } = createTasks[kind];
  const node = makeNode(CanvasNodeType.Config, position);
  return { ...node, title: legacyScript ? '脚本（旧版）' : title, width, height, metadata: { ...node.metadata, generationMode, dramaTask: legacyScript ? 'text' : task, ...(kind === 'script' ? { scriptVersion: legacyScript ? 'legacy' : 'current' } : {}) } };
}
function applyNodeAsset(node: CanvasNodeData, asset: SceneTurnAsset, preserveSize = false): CanvasNodeData {
  const naturalWidth = Number(asset.metadata?.width) || undefined;
  const naturalHeight = Number(asset.metadata?.height) || undefined;
  const scale = !preserveSize && naturalWidth && naturalHeight ? Math.min(350 / naturalWidth, 720 / naturalHeight) : undefined;
  const dimensions = scale && naturalWidth && naturalHeight ? { width: Math.max(96, Math.round(naturalWidth * scale)), height: Math.max(64, Math.round(naturalHeight * scale)) } : {};
  return {
    ...node, ...dimensions, title: asset.name,
    metadata: {
      ...node.metadata, naturalWidth, naturalHeight, durationMs: Number(asset.metadata?.duration) * 1000 || undefined,
      content: asset.type === 'text' ? String(asset.metadata?.content || '') : assetContentUrl(asset.id),
      storageKey: asset.id, status: 'success',
    },
  };
}
const canvasResourceKinds: CanvasResourceKind[] = ['text', 'image', 'video', 'audio'];
const videoCanvasReferenceKinds: CanvasResourceKind[] = ['text', 'image', 'video'];
function isVideoGenerator(node: CanvasNodeData) {
  return nodeDramaTask(node).id === 'clip' && (node.type === CanvasNodeType.Config && !node.metadata?.productionTool ||
    node.type === CanvasNodeType.Video && !node.metadata?.pendingResult && !node.metadata?.storageKey && !node.metadata?.content);
}
function referenceKinds(node: CanvasNodeData, nodes: CanvasNodeData[], connections: CanvasConnection[] = []): CanvasResourceKind[] {
  if (node.type === CanvasNodeType.Group) return getGroupResourceNodes(node.id, nodes, connections).flatMap((member) => referenceKinds(member, nodes));
  const kind = canvasGeneratorOutputKind(node) ?? getCanvasResourceKind(node) ?? canvasResourceKinds.find((item) => item === node.type);
  return kind ? [kind] : [];
}
function acceptsReference(option: LinkedGenerationOption, kinds: CanvasResourceKind[], videoKinds: CanvasResourceKind[] = ['text']) {
  if (!kinds.length) return false;
  if (option.kind === 'join') return kinds.every(k => k === 'video' || k === 'audio');
  if (option.kind === 'audio') return kinds.every(k => k === 'text' || k === 'audio');
  if (option.kind === 'text' || option.kind === 'script') return kinds.every((kind) => kind === 'text' || kind === 'image');
  if (option.kind === 'image') return kinds.every((kind) => kind === 'text' || kind === 'image');
  return kinds.every((kind) => videoKinds.includes(kind));
}
const linkedGenerationOptions: LinkedGenerationOption[] = [
  { kind: 'text', task: 'text' },
  { kind: 'image', task: 'shotImage' },
  { kind: 'video', task: 'clip' },
  { kind: 'join', task: 'join' },
  { kind: 'audio', task: 'text' },
  { kind: 'script', task: 'plot' },
];
function clearDisconnectedVideoReferences(node: CanvasNodeData, sourceIds: Set<string>, nodes: CanvasNodeData[], connections: CanvasConnection[]): CanvasNodeData {
  const metadata = node.metadata;
  if (!metadata) return node;
  const disconnected = (sourceId?: string) => {
    if (!sourceId || !sourceIds.has(sourceId)) return false;
    const sourceGroupId = nodes.find((item) => item.id === sourceId)?.metadata?.groupId;
    return !connections.some((edge) => edge.toNodeId === node.id && (edge.fromNodeId === sourceId || edge.fromNodeId === sourceGroupId));
  };
  const first = disconnected(metadata.firstFrameNodeId);
  const last = disconnected(metadata.lastFrameNodeId);
  const video = disconnected(metadata.referenceVideoNodeId);
  const imageIds = metadata.videoReferenceImageNodeIds?.filter((id) => !disconnected(id));
  if (!first && !last && !video && imageIds?.length === metadata.videoReferenceImageNodeIds?.length) return node;
  return { ...node, metadata: { ...metadata, firstFrameNodeId: first ? undefined : metadata.firstFrameNodeId, lastFrameNodeId: last ? undefined : metadata.lastFrameNodeId, referenceVideoNodeId: video ? undefined : metadata.referenceVideoNodeId, videoReferenceImageNodeIds: imageIds } };
}
const flattenNodeInputs = (inputs: NodeGenerationInput[]) => inputs.flatMap((input) => input.type === 'group' ? input.children : [input]);

function videoReferenceImageIds(metadata: CanvasNodeMetadata | undefined, inputs: ReturnType<typeof flattenNodeInputs>) {
  const imageNodeIds = inputs.filter((item) => item.type === 'image' && item.image?.storageKey).map((item) => item.nodeId);
  return canvasVideoReferenceImageIds(metadata, imageNodeIds);
}

function generationInputProblem(node: CanvasNodeData, nodes: CanvasNodeData[], connections: CanvasConnection[], profile?: ModelProfile) {
  const task = nodeDramaTask(node);
  const resources = buildNodeGenerationInputs(node.id, nodes, connections);
  const linkedMedia = flattenNodeInputs(resources);
  if (task.id === 'join') {
    return new Set(linkedMedia.filter((input) => input.type === 'video').map((input) => input.nodeId)).size >= 2 ? '' : '请连接至少两个已有视频素材的节点。';
  }
  const prompt = rawPrompt(node);
  const tokens = [...prompt.matchAll(/@\[node:([^\]]+)\]/g)];
  if (node.type === CanvasNodeType.Config && node.metadata?.composerContent?.trim() && tokens.length) {
    const inputIds = new Set(resources.map((input) => input.nodeId));
    if (tokens.some((token) => !inputIds.has(token[1]))) return '组合输入引用了未连接或已移除的节点，请重新选择参考。';
  }
  const context = buildNodeGenerationContext(node.id, nodes, connections, prompt);
  if (Array.from(prompt).length > 20000 || Array.from(context.prompt.trim()).length > 20000) return '本次完整输入超过20000个字符，请缩短要求或参考文字。';
  if (task.kind === 'image' && (context.referenceVideos.length || context.referenceAudios.length)) return '图片生成支持文字和参考图，请移除视频或音频引用。';
  if (task.kind === 'image' && profile?.capabilities.maxReferences !== undefined && context.referenceImages.length > profile.capabilities.maxReferences) return `所选模型最多引用 ${profile.capabilities.maxReferences} 张图片，请减少参考图。`;
  const mode = node.metadata?.videoSourceMode ?? 'text';
  const activeVideoMedia = new Set<string | undefined>();
  if (task.kind === 'text') {
    if (context.referenceVideos.length || context.referenceAudios.length) return '请选择支持当前媒体的生成方式。';
    if (context.referenceImages.length > (profile?.capabilities.maxReferences || 0)) return profile?.capabilities.maxReferences ? `当前文字模型最多接收 ${profile.capabilities.maxReferences} 张参考图。` : '当前文字模型接收文本。图片参考可选择千问。';
  } else if (task.kind === 'video' && mode === 'frames') {
    const first = node.metadata?.firstFrameNodeId, last = node.metadata?.lastFrameNodeId;
    if (!profile?.capabilities.firstFrame || (last && !profile.capabilities.lastFrame)) return '请选择支持所设首尾帧的模型。';
    const eligible = linkedMedia.filter((input) => input.type === 'image' && input.image?.storageKey);
    if (!eligible.some((item) => item.nodeId === first)) {
      const connected = connections.some((edge) => edge.toNodeId === node.id && edge.fromNodeId === first);
      if (first && connected) return '首帧图片还未生成或上传，保存图片后可继续。';
      return '请连接已保存的图片，并指定首帧。';
    }
    if (last && !eligible.some((item) => item.nodeId === last)) return '请重新选择已连接的尾帧图片。';
    activeVideoMedia.add(first).add(last);
  } else if (task.kind === 'video' && (mode === 'reference' || mode === 'edit' || mode === 'extend')) {
    if (!profile?.capabilities.videoInputModes?.includes(mode)) return '请选择支持该视频输入方式的模型。';
    const videoId = node.metadata?.referenceVideoNodeId;
    const limit = profile.capabilities.maxReferenceImages ?? 0;
    const imageIds = limit ? videoReferenceImageIds(node.metadata, linkedMedia) : [];
    if (imageIds.length > limit) return `所选模型最多使用 ${limit} 张参考图，请减少已选图片。`;
    if ((videoId || mode !== 'reference' || !imageIds.length) && !linkedMedia.some((item) => item.type === 'video' && item.video?.storageKey && item.nodeId === videoId)) {
      return mode === 'reference' ? '请选用至少一张参考图或一段参考视频。' : '请连接已保存的视频，并指定原视频。';
    }
    activeVideoMedia.add(videoId);
    imageIds.forEach((id) => activeVideoMedia.add(id));
  }
  if (task.kind === 'video' && node.metadata?.composerContent?.trim() && tokens.length) {
    const mentioned = new Set(tokens.map((token) => token[1]));
    const mentionedMedia = flattenNodeInputs(resources.filter((input) => mentioned.has(input.nodeId)));
    if (mentionedMedia.some((input) => input.type !== 'text' && !activeVideoMedia.has(input.nodeId))) return '提示词引用了本次未选用的素材，请为素材设置用途或删除相应引用。';
  }
  return context.prompt.trim() ? '' : '请填写生成要求，或连接包含内容的文字节点。';
}
function placeResult(source: CanvasNodeData, nodes: CanvasNodeData[], type: CanvasNodeType = CanvasNodeType.Video) {
  const output = makeNode(type, { x: source.position.x + source.width + 96, y: source.position.y });
  let overlap = nodes.find((node) => output.position.x < node.position.x + node.width + 28 && output.position.x + output.width + 28 > node.position.x && output.position.y < node.position.y + node.height + 28 && output.position.y + output.height + 28 > node.position.y);
  while (overlap) {
    output.position.y = overlap.position.y + overlap.height + 28;
    overlap = nodes.find((node) => output.position.x < node.position.x + node.width + 28 && output.position.x + output.width + 28 > node.position.x && output.position.y < node.position.y + node.height + 28 && output.position.y + output.height + 28 > node.position.y);
  }
  return output;
}
function projectCanvasNode(node: CanvasNodeData, tasks: GenerationView[]): CanvasNodeData {
  const task = tasks.find((item) => item.outputNodeId === node.id || (!item.outputNodeId && item.sourceNodeId === node.id && node.type === CanvasNodeType.Video));
  const media = node.metadata?.storageKey && node.type !== CanvasNodeType.Text ? { content: assetContentUrl(node.metadata.storageKey) } : {};
  if (!task || node.type !== CanvasNodeType.Video) return Object.keys(media).length ? { ...node, metadata: { ...node.metadata, ...media } } : node;
  if (task.status === 'ready' && task.assetId) return { ...node, metadata: { ...node.metadata, content: assetContentUrl(task.assetId), storageKey: task.assetId, videoTaskId: task.id, status: 'success', pendingResult: false } };
  if (['failed', 'query_interrupted', 'submission_unknown'].includes(task.status)) {
    const problem = typeof task.error === 'string' ? task.error : task.error?.message;
    return { ...node, metadata: { ...node.metadata, ...media, status: 'error', errorDetails: `${taskLabels[task.status]}${problem ? `：${problem}` : ''}` } };
  }
  return { ...node, metadata: { ...node.metadata, ...media, status: ['queued', 'submitting', 'processing', 'downloading'].includes(task.status) ? 'loading' : 'idle' } };
}

function hasGenerationContent(node: CanvasNodeData) {
  return node.type === CanvasNodeType.Text ? Boolean(node.metadata?.content) : Boolean(node.metadata?.storageKey);
}

function canResumeVideo(task: GenerationView) {
  return (task.canResume ?? Boolean(task.remoteTaskId)) && ['query_interrupted', 'processing', 'downloading', 'submission_unknown'].includes(task.status);
}

const activeVideoStatuses = new Set(['queued', 'submitting', 'processing', 'downloading']);
const activeTextStatuses = new Set(['queued', 'submitting', 'running']);
const activeImageStatuses = new Set(['queued', 'claimed', 'submitting', 'response_saved']);
type GenerationKind = 'video' | 'text' | 'image';

export interface MaterialCanvasHandle { save: () => Promise<void> }

const legacyScript = z.object({ plot: z.string(), scenes: z.array(z.object({ heading: z.string(), action: z.string(), dialogues: z.array(z.object({ speaker: z.string(), line: z.string() })) })) });

function projectLegacyMaterialNode(node: CanvasNodeData): CanvasNodeData {
  const artifact = node.metadata?.creativeArtifact;
  if (node.metadata?.publicMaterialRole || typeof artifact?.role !== 'string') return node;
  const role = artifact.role;
  const script = role === 'writer' ? legacyScript.parse(artifact) : undefined;
  const content = script ? [script.plot, ...script.scenes.flatMap((scene) => [scene.heading, scene.action, ...scene.dialogues.map((line) => `${line.speaker}：${line.line}`)])].join('\n\n') : '';
  return { ...node, metadata: { ...node.metadata, publicMaterialRole: role, content, texts: [], primaryTextId: undefined } };
}

export default function StudioPage({ projectId, initialSceneId, embedded = false, saveRef }: { projectId: string; initialSceneId?: string; embedded?: boolean; saveRef?: React.Ref<MaterialCanvasHandle> }) {
  const { user, error: userError } = useWorkspaceUser();
  const [envelope, setEnvelope] = useState<ProjectEnvelope | null>(null);
  const envelopeRef = useRef<ProjectEnvelope | null>(null);
  const [sceneId, setSceneId] = useState(initialSceneId || '');
  const [canvas, setCanvas] = useState<CanvasSnapshot>(emptyCanvas(initialSceneId || ''));
  const canvasRef = useRef(canvas);
  const visibleNodeIdsRef = useRef(new Map<string, string>());
  const savedCanvasRef = useRef(canvas);
  const editVersion = useRef(0);
  const contentVersion = useRef(0);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const [inputDirty, setInputDirty] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [previewedResultIds, setPreviewedResultIds] = useState<Record<string, string>>({});
  const [tasks, setTasks] = useState<GenerationView[]>([]);
  const [textTasks, setTextTasks] = useState<TextGenerationView[]>([]);
  const [imageTasks, setImageTasks] = useState<CanvasImageJob[]>([]);
  const taskRefresh = useRef<Record<GenerationKind, () => Promise<void>> | null>(null);
  const [textSettings, setTextSettings] = useState<TextSettings | null>(null);
  const syncedTextRevisions = useRef(new Set<string>());
  const syncedImages = useRef(new Set<string>());
  const [model, setModel] = useState('');
  const [providerCatalog, setProviderCatalog] = useState<ProviderCatalog | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [loadingCanvas, setLoadingCanvas] = useState(true);
  const [miniMapOpen, setMiniMapOpen] = useState(false);
  const [newSceneTitle, setNewSceneTitle] = useState('');
  const [referencePickerId, setReferencePickerId] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionHandle | null>(null);
  const [connectionCursor, setConnectionCursor] = useState<{ position: Position; targetId?: string }>({ position: { x: 0, y: 0 } });
  const [connectionCreate, setConnectionCreate] = useState<PendingConnectionCreate | null>(null);
  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null);
  const [createPosition, setCreatePosition] = useState<Position | null>(null);
  const [railMenu, setRailMenu] = useState<Position | null>(null);
  const [zoomMenu, setZoomMenu] = useState(false);
  const [showLinks, setShowLinks] = useState(true);
  const [canvasView, setCanvasView] = useState<'workflow' | 'storyboard'>('workflow');
  const [assetPane, setAssetPane] = useState<'nodes' | 'assets' | null>(null);
  const [panel, setPanel] = useState<'tools' | 'help' | 'keys' | 'pointer' | 'character' | 'saveAsset' | 'versions' | null>(null);
  const [snap, setSnap] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [storyFilter, setStoryFilter] = useState('全部');
  const [showStoryAssets, setShowStoryAssets] = useState(true);
  const [chatPlacement, setChatPlacement] = useState<'left' | 'right' | 'floating'>('right');
  const chatOpen = useMaterialWorkspace(state => state.chatOpen);
  const clipboard = useRef<{ nodes: CanvasNodeData[]; connections: CanvasConnection[]; sceneId: string; projectId: string } | null>(null);
  const shellRef = useRef<HTMLDivElement>(null);
  const canvasFocusFrame = useRef<number | null>(null);
  const [libraryMode, setLibraryMode] = useState<'assets' | 'history' | null>(null);
  const [libraryError, setLibraryError] = useState('');
  const [scriptTableId, setScriptTableId] = useState<string | null>(null);
  const [productionEditorId, setProductionEditorId] = useState<string | null>(null);
  const pickerPosition = useRef<Position | undefined>(undefined);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [resizing, setResizing] = useState(false);
  const [expandedBatches, setExpandedBatches] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<CanvasNodeData | null>(null);
  const [reviewTaskId, setReviewTaskId] = useState<string | null>(null);
  const [splitNode, setSplitNode] = useState<CanvasNodeData | null>(null);
  const [directoryEdit, setDirectoryEdit] = useState<{ node: CanvasNodeData; action: string } | null>(null);
  const [mediaDurations, setMediaDurations] = useState<Record<string, { src: string; seconds: number; width: number; height: number }>>({});
  const containerRef = useRef<HTMLDivElement>(null);
  const [canvasSize, setCanvasSize] = useState({ width: 600, height: 640 });
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadTarget = useRef<string | null>(null);
  const mediaReferenceTarget = useRef<string | null>(null);
  const theme = canvasThemes[useThemeStore((state) => state.theme)];

  const acceptEnvelope = useCallback((value: ProjectEnvelope) => {
    const accepted = acceptProjectEnvelope(value);
    envelopeRef.current = accepted; setEnvelope(accepted);
  }, []);
  const mergeServerCanvas = useCallback((next: ProjectEnvelope, targetSceneId: string) => {
    const remote = next.canvases.find((item) => item.sceneId === targetSceneId);
    if (!remote || canvasRef.current.sceneId !== targetSceneId || savedCanvasRef.current.sceneId !== targetSceneId || next.revision < (envelopeRef.current?.revision || 0)) return false;
    const merged = mergeCanvasSnapshots(savedCanvasRef.current, canvasRef.current, remote);
    const changed = !sameValue(merged, remote);
    acceptEnvelope(next); savedCanvasRef.current = remote; canvasRef.current = merged; setCanvas(merged);
    dirtyRef.current = changed; setDirty(changed);
    setInputDirty(!sameValue({ nodes: merged.nodes, connections: merged.connections }, { nodes: remote.nodes, connections: remote.connections }));
    return true;
  }, [acceptEnvelope]);
  const changeCanvas = useCallback((update: CanvasSnapshot | ((value: CanvasSnapshot) => CanvasSnapshot), viewOnly = false) => {
    const value = typeof update === 'function' ? update(canvasRef.current) : update;
    canvasRef.current = value; editVersion.current += 1;
    if (!viewOnly) { contentVersion.current += 1; setInputDirty(true); }
    setCanvas(value); dirtyRef.current = true; setDirty(true); setMessage('');
  }, []);
  const changeNode = useCallback((id: string, patch: Partial<CanvasNodeData>) => changeCanvas((value) => ({ ...value, nodes: value.nodes.map((node) => node.id === id ? { ...node, ...patch } : node) })), [changeCanvas]);
  const changeMetadata = useCallback((id: string, patch: Partial<CanvasNodeMetadata>) => changeCanvas((value) => ({
    ...value,
    nodes: value.nodes.map((node) => {
      if (node.id !== id) return node;
      const metadata = { ...node.metadata, ...patch };
      const content = patch.content;
      if (node.type === 'text' && typeof content === 'string' && metadata.texts?.length) {
        const primaryId = metadata.primaryTextId || metadata.texts[0].id;
        metadata.texts = metadata.texts.map((item) => item.id === primaryId ? { ...item, content } : item);
      }
      return { ...node, metadata };
    }),
  })), [changeCanvas]);
  const closeConnectionCreate = useCallback(() => setConnectionCreate(null), []);
  const selectNode = useCallback((id: string) => { setSelectedId(id); setSelectedIds(new Set([id])); setSelectedConnectionId(null); setCreatePosition(null); setConnectionCreate(null); }, []);
  const resolveCanvasLink = useCallback((handle: ConnectionHandle, targetId: string, nodes: CanvasNodeData[]) => {
    const edge = normalizeConnection(handle.nodeId, targetId, nodes, handle.handleType);
    if (!edge) return null;
    const from = nodes.find((node) => node.id === edge.fromNodeId);
    const to = nodes.find((node) => node.id === edge.toNodeId);
    if (!from || !to || from.type === 'agent' || to.type === 'agent') return null;
    const kinds = referenceKinds(from, nodes, canvasRef.current.connections);
    if (to.metadata?.productionTool==='audio-edit') return kinds.length && kinds.every(k=>k==='audio')?edge:null;
    if (to.metadata?.productionTool==='director') return kinds.length && kinds.every(k=>k==='image')?edge:null;
    if (to.type !== CanvasNodeType.Config && !(to.type === CanvasNodeType.Text && to.metadata?.generationMode === 'text')) return edge;
    const task = nodeDramaTask(to);
    const option = { kind: task.id === 'join' ? 'join' as const : task.kind, task: task.id };
    return acceptsReference(option, kinds, videoCanvasReferenceKinds) ? edge : null;
  }, []);
  const withLinkedVideoInputs = useCallback((target: CanvasNodeData, sources: CanvasNodeData[], nodes: CanvasNodeData[]): CanvasNodeData => {
    if (!isVideoGenerator(target)) return target;
    const media = sources.flatMap((source) => source.type === CanvasNodeType.Group ? getGroupResourceNodes(source.id, nodes, canvasRef.current.connections) : [source]);
    const image = media.find((source) => referenceKinds(source, nodes).includes('image'));
    const video = media.find((source) => referenceKinds(source, nodes).includes('video'));
    if (!image && !video) return target;
    const metadata = target.metadata;
    const currentMode = metadata?.videoSourceMode ?? 'text';
    if (currentMode === 'frames') {
      return image && !metadata?.firstFrameNodeId ? { ...target, metadata: { ...metadata, firstFrameNodeId: image.id } } : target;
    }
    if (currentMode !== 'text') {
      return video && !metadata?.referenceVideoNodeId ? { ...target, metadata: { ...metadata, referenceVideoNodeId: video.id } } : target;
    }
    const models = providerCatalog?.models.filter((item) => item.kind === 'video') ?? [];
    const selectedId = metadata?.modelProfileId ?? providerCatalog?.defaults.video;
    const selected = models.find((item) => item.id === selectedId);
    const candidates = [selected, ...models.filter((item) => item !== selected && item.available)].filter((item): item is ModelProfile => Boolean(item));
    const imageCount = media.filter((source) => referenceKinds(source, nodes).includes('image')).length;
    const modeFor = (profile: ModelProfile): CanvasVideoSourceMode | undefined => {
      const mixed = canvasVideoModeSupported('reference', profile) && (!image || profile.capabilities.maxReferenceImages);
      if (mixed && (video || imageCount > 1)) return 'reference';
      if (image && canvasVideoModeSupported('frames', profile)) return 'frames';
      return mixed ? 'reference' : undefined;
    };
    const model = metadata?.modelProfileId ? selected : candidates.find(modeFor);
    const mode = model ? modeFor(model) : providerCatalog ? undefined : image ? 'frames' : 'reference';
    if (!mode) return target;
    return { ...target, metadata: {
      ...metadata, videoSourceMode: mode, videoMode: undefined,
      modelProfileId: model?.id === providerCatalog?.defaults.video ? undefined : model?.id ?? metadata?.modelProfileId,
      ...(mode === 'frames' && image ? { firstFrameNodeId: image.id } : {}),
      ...(mode === 'reference' && video ? { referenceVideoNodeId: video.id } : {}),
    } };
  }, [providerCatalog]);
  const appendCanvasLink = useCallback((edge: Omit<CanvasConnection, 'id'>) => {
    const visibleId = (id: string) => visibleNodeIdsRef.current.get(id) ?? id;
    if (canvasRef.current.connections.some((item) => visibleId(item.fromNodeId) === visibleId(edge.fromNodeId) && visibleId(item.toNodeId) === visibleId(edge.toNodeId))) return;
    changeCanvas((value) => {
      const source = value.nodes.find((node) => node.id === edge.fromNodeId);
      if (!source) return value;
      return {
        ...value,
        nodes: value.nodes.map((node) => node.id === edge.toNodeId ? withLinkedVideoInputs(node, [source], value.nodes) : node),
        connections: [...value.connections, { ...edge, id: operationId() }],
      };
    });
  }, [changeCanvas, withLinkedVideoInputs]);
  const removeNodes = useCallback((ids: string[]) => {
    if (busyRef.current || loadingCanvas) return;
    const deletedIds = new Set(ids.filter((id) => canvasRef.current.nodes.some((node) => node.id === id)));
    if (!deletedIds.size || !window.confirm(`删除选中的 ${deletedIds.size} 个画布节点及其连线？生成任务、素材和历史记录继续保存。`)) return;
    for (const [resultId, sourceId] of visibleNodeIdsRef.current) {
      if (deletedIds.has(sourceId)) deletedIds.add(resultId);
    }
    changeCanvas((value) => {
      const disconnectedIds = new Set(deletedIds);
      const nodes = value.nodes.filter((node) => !deletedIds.has(node.id)).map((node) => {
        if (!node.metadata?.groupId || !deletedIds.has(node.metadata.groupId)) return node;
        disconnectedIds.add(node.id);
        return { ...node, metadata: { ...node.metadata, groupId: undefined } };
      });
      const connections = value.connections.filter((edge) => !deletedIds.has(edge.fromNodeId) && !deletedIds.has(edge.toNodeId));
      return { ...value, nodes: nodes.map((node) => clearDisconnectedVideoReferences(node, disconnectedIds, nodes, connections)), connections };
    });
    setSelectedId(null); setSelectedIds(new Set()); setSelectedConnectionId(null); setConnection(null); setConnectionCreate(null);
    setReferencePickerId((current) => current && deletedIds.has(current) ? null : current);
  }, [changeCanvas, loadingCanvas]);
  useEffect(() => {
    const remove = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing || event.key !== 'Delete' && event.key !== 'Backspace') return;
      const target = event.target instanceof Element ? event.target : null;
      if (!target?.closest('[data-testid="canvas-node"],[data-testid="canvas-node-actions"]') || target.closest('input,textarea,select,[contenteditable="true"],[role="textbox"],.ant-modal,.ant-popover')) return;
      const focusedNodeId = target.closest<HTMLElement>('[data-testid="canvas-node"]')?.dataset.nodeId;
      if (focusedNodeId && !selectedIds.has(focusedNodeId)) return;
      if (!selectedIds.size || referencePickerId || loadingCanvas || busyRef.current) return;
      event.preventDefault(); removeNodes([...selectedIds]);
    };
    window.addEventListener('keydown', remove); return () => window.removeEventListener('keydown', remove);
  }, [selectedIds, referencePickerId, loadingCanvas, removeNodes]);
  const screenToCanvas = useCallback((clientX: number, clientY: number) => {
    const rect = containerRef.current?.getBoundingClientRect();
    const viewport = canvasRef.current.viewport;
    return { x: (clientX - (rect?.left || 0) - viewport.x) / viewport.k, y: (clientY - (rect?.top || 0) - viewport.y) / viewport.k };
  }, []);
  const openLinkedMenu = useCallback((handle: ConnectionHandle, clientX: number, clientY: number) => {
    const surface = containerRef.current;
    const { nodes } = canvasRef.current;
    const source = nodes.find((node) => node.id === handle.nodeId);
    if (!surface || !source || handle.handleType !== 'source') return;
    const sources = selectedIds.size > 1 && selectedIds.has(source.id) ? nodes.filter(node => selectedIds.has(node.id)) : [source];
    const kinds = [...new Set(sources.flatMap(node => referenceKinds(node, nodes, canvasRef.current.connections)))];
    const options = linkedGenerationOptions.filter(option => acceptsReference(option, kinds, videoCanvasReferenceKinds));
    if (!options.length) return;
    const rect = surface.getBoundingClientRect();
    const menuX = Math.max(rect.left + 8, Math.min(rect.right - 216, clientX));
    const menuY = Math.max(rect.top + 8, Math.min(rect.bottom - 252, clientY));
    const kind = referenceKinds(source, nodes, canvasRef.current.connections)[0];
    const sourceLabels = { text: '文本', image: '图片', video: '视频', audio: '音频' };
    setConnectionCreate({ connection: handle, sourceIds: sources.map(node => node.id), options, title: sources.length > 1 ? `引用 ${sources.length} 个节点` : kind ? `引用${sourceLabels[kind]}节点` : '引用选中的节点', position: screenToCanvas(clientX, clientY), menuPosition: screenToCanvas(menuX, menuY) });
  }, [selectedIds, screenToCanvas]);
  const cancelCanvasFocus = useCallback(() => {
    if (canvasFocusFrame.current === null) return;
    cancelAnimationFrame(canvasFocusFrame.current);
    canvasFocusFrame.current = null;
  }, []);
  useEffect(() => cancelCanvasFocus, [cancelCanvasFocus]);
  const focusNode = useCallback((resultId: string) => {
    const id = visibleNodeIdsRef.current.get(resultId) ?? resultId;
    const node = canvasRef.current.nodes.find((item) => item.id === id);
    if (!node) { setError('结果节点已移除，任务和视频仍保留在来源节点的记录中。'); return; }
    if (id !== resultId) setPreviewedResultIds(current => ({ ...current, [id]: resultId }));
    selectNode(id);
    cancelCanvasFocus();
    canvasFocusFrame.current = requestAnimationFrame(() => {
      canvasFocusFrame.current = null;
      const container = containerRef.current;
      if (!container) return;
      const panel = container.querySelector<HTMLElement>(`[data-node-panel="${id}"]`);
      const toolbar = container.parentElement?.querySelector<HTMLElement>('[data-canvas-top-toolbar]');
      const viewport = focusViewportForNode(node, { width: container.clientWidth, height: container.clientHeight }, { above: (toolbar?.getBoundingClientRect().height || 54) + 28, below: panel?.getBoundingClientRect().height || 0, bottomInset: 64 });
      const current = canvasRef.current.viewport;
      if (current.x !== viewport.x || current.y !== viewport.y || current.k !== viewport.k) changeCanvas((value) => ({ ...value, viewport }), true);
    });
  }, [cancelCanvasFocus, changeCanvas, selectNode]);
  function fitAllNodes() {
    cancelCanvasFocus();
    const nodes = canvasRef.current.nodes.filter(node => !visibleNodeIdsRef.current.has(node.id));
    if (!nodes.length) return;
    const container = containerRef.current;
    if (!container) return;
    const bounds = nodeBounds(nodes);
    const toolbar = container.parentElement?.querySelector<HTMLElement>('[data-canvas-top-toolbar]');
    const top = (toolbar?.getBoundingClientRect().height || 54) + 28;
    const panelHeight = container.querySelector<HTMLElement>('[data-node-panel]')?.getBoundingClientRect().height || 0;
    const height = container.clientHeight - top - 80 - panelHeight;
    const k = Math.min(1, (container.clientWidth - 48) / (bounds.right - bounds.left), height / (bounds.bottom - bounds.top));
    changeCanvas((value) => ({ ...value, viewport: { k, x: container.clientWidth / 2 - (bounds.left + bounds.right) / 2 * k, y: top + height / 2 - (bounds.top + bounds.bottom) / 2 * k } }), true);
  }
  function zoomAtCenter(scale: number) {
    if (!Number.isFinite(scale) || scale < 0.05 || scale > 5) return;
    changeCanvas(value => {
      const { x, y, k } = value.viewport;
      const cx = canvasSize.width / 2, cy = canvasSize.height / 2;
      return { ...value, viewport: { x: cx - (cx - x) * scale / k, y: cy - (cy - y) * scale / k, k: scale } };
    }, true);
  }

  useEffect(() => {
    if (!user || !projectId) return;
    let active = true;
    getCloudProject(projectId).then((value) => { if (active) { acceptEnvelope(value); setSceneId((current) => current || value.project.graphData.nodes.find((node) => node.type === 'scene')?.id || ''); } }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    getWanSettings().then((value) => { if (active) setModel(value.model); }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    getProviderCatalog(projectId).then((value) => { if (active) setProviderCatalog(value); }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    getTextSettings().then((value) => { if (active) setTextSettings(value); }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    return () => { active = false; };
  }, [user, projectId, acceptEnvelope]);
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const measure = () => setCanvasSize({ width: container.clientWidth, height: container.clientHeight });
    measure();
    const observer = new ResizeObserver(measure); observer.observe(container);
    return () => observer.disconnect();
  }, [user, sceneId]);
  useEffect(() => {
    if (!user || !projectId || !sceneId) return;
    let active = true; setLoadingCanvas(true);
    getCloudCanvas(projectId, sceneId).then((value) => {
      if (!active) return;
      canvasRef.current = value.canvas; savedCanvasRef.current = value.canvas; setCanvas(value.canvas); dirtyRef.current = false; setDirty(false); setInputDirty(false); setSelectedId(null); setSelectedIds(new Set()); setReferencePickerId(null); setConnection(null); setConnectionCreate(null); editVersion.current = 0; contentVersion.current = 0;
      const current = envelopeRef.current; if (current) acceptEnvelope({ ...current, revision: value.revision });
      window.history.replaceState(null, '', `${window.location.pathname}?id=${encodeURIComponent(projectId)}&sceneId=${encodeURIComponent(sceneId)}`);
    }).catch((problem: unknown) => { if (active) setError(errorText(problem)); }).finally(() => { if (active) setLoadingCanvas(false); });
    return () => { active = false; };
  }, [user, projectId, sceneId, acceptEnvelope]);
  useEffect(() => {
    if (!user || !projectId || !sceneId) return;
    let active = true;
    const requests = { video: 0, text: 0, image: 0 };
    const refresh = (kind: GenerationKind, read: (isCurrent: () => boolean) => Promise<void>) => async () => {
      if (!active || busyRef.current) return;
      const request = ++requests[kind];
      const isCurrent = () => active && request === requests[kind] && !busyRef.current;
      try {
        await read(isCurrent);
      } catch (problem: unknown) {
        if (isCurrent()) setError(errorText(problem));
      }
    };
    const video = refresh('video', async (isCurrent) => {
      const { generations } = await getGenerations(projectId);
      if (isCurrent()) setTasks([...generations].sort((a, b) => b.createdAt.localeCompare(a.createdAt)));
    });
    const text = refresh('text', async (isCurrent) => {
      const result = await getTextGenerations(projectId);
      if (!isCurrent()) return;
      setTextTasks([...result.textGenerations].sort((a, b) => b.createdAt.localeCompare(a.createdAt)));
      const applied = result.textGenerations.filter((task) => task.sceneId === sceneId && task.appliedRevision !== null && !syncedTextRevisions.current.has(`${task.id}:${task.appliedRevision}`));
      if (!applied.length) return;
      const next = await getCloudProject(projectId);
      if (!isCurrent() || !mergeServerCanvas(next, sceneId)) return;
      if (dirtyRef.current) setMessage('文字结果与服务端版本已同步，本地编辑和视口调整继续保留，可继续保存画布。');
      applied.forEach((task) => syncedTextRevisions.current.add(`${task.id}:${task.appliedRevision}`));
    });
    const image = refresh('image', async (isCurrent) => {
      const jobs = await getCanvasImageJobs(projectId);
      if (!isCurrent()) return;
      setImageTasks(jobs);
      const ready = jobs.filter((task) => task.sceneId === sceneId && task.status === 'ready' && !syncedImages.current.has(task.id));
      if (!ready.length) return;
      const next = await getCloudProject(projectId);
      if (!isCurrent() || !mergeServerCanvas(next, sceneId)) return;
      ready.forEach((task) => syncedImages.current.add(task.id));
    });
    taskRefresh.current = { video, text, image };
    void video();
    void text();
    void image();
    return () => {
      active = false;
      taskRefresh.current = null;
    };
  }, [user, projectId, sceneId, mergeServerCanvas]);
  const syncSavedResults = useEffectEvent(() => {
    if (textTasks.some((task) => task.sceneId === sceneId && task.appliedRevision !== null && !syncedTextRevisions.current.has(`${task.id}:${task.appliedRevision}`))) {
      void taskRefresh.current?.text();
    }
    if (imageTasks.some((task) => task.sceneId === sceneId && task.status === 'ready' && !syncedImages.current.has(task.id))) {
      void taskRefresh.current?.image();
    }
  });
  useEffect(() => {
    if (!loadingCanvas && !busy) syncSavedResults();
  }, [loadingCanvas, busy]);
  useTaskPolling(Boolean(user && sceneId && tasks.some((task) => task.sceneId === sceneId && (
    activeVideoStatuses.has(task.status) || (task.status === 'submission_unknown' && task.remoteTaskId)
  ))), 2500, () => taskRefresh.current?.video());
  useTaskPolling(Boolean(user && sceneId && textTasks.some((task) => task.sceneId === sceneId && activeTextStatuses.has(task.status))), 2500, () => taskRefresh.current?.text());
  useTaskPolling(Boolean(user && sceneId && imageTasks.some((task) => task.sceneId === sceneId && activeImageStatuses.has(task.status))), 2500, () => taskRefresh.current?.image());
  const imageNeedsConnection = imageTasks.some((task) => task.sceneId === sceneId && task.status === 'waiting_connection');
  useEffect(() => {
    if (!imageNeedsConnection) return;
    const refreshConnection = () => { void taskRefresh.current?.image(); };
    window.addEventListener('focus', refreshConnection);
    return () => window.removeEventListener('focus', refreshConnection);
  }, [imageNeedsConnection]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', guard); return () => window.removeEventListener('beforeunload', guard);
  }, [dirty]);
  useEffect(() => {
    if (!connection) return;
    const cancel = () => setConnection(null);
    const readDrop = (event: MouseEvent) => {
      const surface = containerRef.current;
      const element = document.elementFromPoint(event.clientX, event.clientY);
      if (!surface || !element || !surface.contains(element) || element.closest('[data-node-panel],[data-connection-create-menu]')) return {};
      const node = element.closest<HTMLElement>('[data-node-id]');
      const isSamePort = node?.dataset.testid === `canvas-connection-${connection.handleType}`;
      if (node) return { targetId: isSamePort ? undefined : node.dataset.nodeId };
      if (!element.closest('[data-canvas-no-zoom],[data-connection-id]')) return { surface };
      return {};
    };
    const move = (event: MouseEvent) => {
      const { targetId } = readDrop(event);
      const edge = targetId && resolveCanvasLink(connection, targetId, canvasRef.current.nodes);
      setConnectionCursor({ position: screenToCanvas(event.clientX, event.clientY), targetId: edge ? targetId : undefined });
    };
    const finish = (event: MouseEvent) => {
      cancel();
      const { targetId, surface } = readDrop(event);
      const { nodes } = canvasRef.current;
      if (targetId) {
        const edge = resolveCanvasLink(connection, targetId, nodes);
        if (edge) appendCanvasLink(edge);
        return;
      }
      if (surface) openLinkedMenu(connection, event.clientX, event.clientY);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') cancel(); };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', finish);
    window.addEventListener('keydown', escape);
    window.addEventListener('blur', cancel);
    return () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', finish);
      window.removeEventListener('keydown', escape);
      window.removeEventListener('blur', cancel);
    };
  }, [connection, appendCanvasLink, openLinkedMenu, resolveCanvasLink, screenToCanvas]);

  async function saveCanvas() {
    const activeSceneId = canvasRef.current.sceneId;
    try {
      return await persistCanvas();
    } catch (problem: unknown) {
      if (!(problem instanceof ApiError) || problem.code !== 'REVISION_CONFLICT') throw problem;
      const latest = await getCloudProject(projectId);
      const remote = latest.canvases.find((item) => item.sceneId === activeSceneId);
      if (!remote || canvasRef.current.sceneId !== activeSceneId || savedCanvasRef.current.sceneId !== activeSceneId) {
        throw new Error('场景已经变化，当前草稿已保留。请核对项目中的最新场景。');
      }
      if (!canMergeCanvasEdits(savedCanvasRef.current, canvasRef.current, remote)) {
        throw new Error('另一窗口修改了相同的画布内容。当前草稿已保留。请在另一窗口核对已保存的修改。');
      }
      if (!mergeServerCanvas(latest, activeSceneId)) throw problem;
      const revision = await persistCanvas();
      if (!dirtyRef.current) setMessage('其他窗口的修改已合并，画布已保存。');
      return revision;
    }
  }

  async function persistCanvas() {
    const current = envelopeRef.current;
    if (!current || !canvasRef.current.sceneId) throw new Error('请先选择剧情场景。');
    const version = editVersion.current, savedContentVersion = contentVersion.current;
    const payload = structuredClone(canvasRef.current);
    const result = await saveCloudCanvas(projectId, payload, current.revision);
    savedCanvasRef.current = result.canvas;
    acceptEnvelope({ ...current, revision: result.revision, canvases: [...current.canvases.filter((item) => item.sceneId !== payload.sceneId), result.canvas] });
    if (savedContentVersion === contentVersion.current) setInputDirty(false);
    if (version === editVersion.current) { canvasRef.current = result.canvas; setCanvas(result.canvas); dirtyRef.current = false; setDirty(false); setMessage('剧情画布已保存'); }
    else setMessage('已保存提交时的版本，新的编辑等待保存');
    return result.revision;
  }
  const standaloneCanvas = useRef<MaterialCanvasHandle>(null);
  useImperativeHandle(saveRef ?? standaloneCanvas, () => ({ save: async () => {
    if (busyRef.current) throw new Error('素材操作正在执行，请稍后关闭。');
    if (dirtyRef.current) await saveCanvas();
  } }));
  useEffect(() => {
    if (embedded || !sceneId) return;
    const savePending = async () => { await standaloneCanvas.current?.save(); };
    useMaterialWorkspace.setState({ savePending });
    return () => {
      const workspace = useMaterialWorkspace.getState();
      if (workspace.savePending !== savePending) return;
      if (workspace.scope?.projectId === projectId && workspace.scope.sceneId === sceneId) workspace.close();
      useMaterialWorkspace.setState({ savePending: async () => {} });
    };
  }, [embedded, projectId, sceneId]);
  async function perform(action: () => Promise<void>, refreshKind?: GenerationKind) {
    if (busyRef.current) return;
    const refresh = refreshKind ? taskRefresh.current?.[refreshKind] : undefined;
    busyRef.current = true; setBusy(true); setError(''); setMessage('');
    await action().catch((problem: unknown) => setError(errorText(problem))).finally(() => {
      busyRef.current = false;
      setBusy(false);
      void refresh?.();
    });
  }
  function addNode(type: CanvasCreateKind, position?: Position, legacyScript = false) {
    if (!sceneId || loadingCanvas) return;
    const origin = position || screenToCanvas((containerRef.current?.getBoundingClientRect().left || 0) + 80, (containerRef.current?.getBoundingClientRect().top || 0) + 110);
    const node = nodeForMenu(type, origin, legacyScript);
    if (!position) {
      let overlap = canvasRef.current.nodes.find((item) => item.type !== CanvasNodeType.Group && node.position.x < item.position.x + item.width + 28 && node.position.x + node.width + 28 > item.position.x && node.position.y < item.position.y + item.height + 28 && node.position.y + node.height + 28 > item.position.y);
      while (overlap) {
        node.position.x = overlap.position.x + overlap.width + 96;
        overlap = canvasRef.current.nodes.find((item) => item.type !== CanvasNodeType.Group && node.position.x < item.position.x + item.width + 28 && node.position.x + node.width + 28 > item.position.x && node.position.y < item.position.y + item.height + 28 && node.position.y + node.height + 28 > item.position.y);
      }
    }
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, node] })); setCreatePosition(null); setRailMenu(null); focusNode(node.id);
  }
  function addLinkedGeneration(option: LinkedGenerationOption) {
    if (!connectionCreate || loadingCanvas || busyRef.current) return;
    const { connection: source, position } = connectionCreate;
    const task = findDramaTask(option.task);
    const sourceNode = canvasRef.current.nodes.find((item) => item.id === source.nodeId);
    if (!sourceNode || option.disabled) return;
    let node = option.kind === 'audio' ? nodeForMenu('audio', position) : option.kind === 'script' || task.kind === 'image' || task.kind === 'video'
      ? nodeForMenu(option.kind === 'join' ? 'smart-edit' : option.kind === 'script' ? 'script' : task.kind === 'image' ? 'image' : 'video', position)
      : { ...makeNode(CanvasNodeType.Config, position), width: 350, height: 200 };
    node.position = { x: position.x, y: position.y - node.height / 2 };
    if (option.kind !== 'audio') {
      node.title = task.label;
      node.metadata = { ...node.metadata, generationMode: task.kind, dramaTask: task.id };
    }
    const sources = (connectionCreate.sourceIds ?? [source.nodeId]).flatMap(id => canvasRef.current.nodes.filter(item => item.id === id));
    if (option.kind === 'video') node = withLinkedVideoInputs(node, sources, canvasRef.current.nodes);
    const nodes = [...canvasRef.current.nodes, node];
    const edges = (connectionCreate.sourceIds ?? [source.nodeId]).map(nodeId => resolveCanvasLink({ ...source, nodeId }, node.id, nodes));
    if (edges.some(edge => !edge)) { closeConnectionCreate(); return; }
    changeCanvas((value) => ({ ...value, nodes, connections: [...value.connections, ...edges.flatMap(edge => edge ? [{ ...edge, id: operationId() }] : [])] }));
    focusNode(node.id);
  }
  async function selectScene(id: string) { if (id !== sceneId) await perform(async () => { if (dirty) await saveCanvas(); setSceneId(id); }); }
  async function createScene(event: React.FormEvent) {
    event.preventDefault();
    await perform(async () => {
      if (dirty) await saveCanvas();
      const current = envelopeRef.current; if (!current) throw new Error('项目尚未加载。');
      const result = await createCloudScene(projectId, newSceneTitle, current.revision); acceptEnvelope(result); setNewSceneTitle('');
      const added = result.project.graphData.nodes.find((node) => node.type === 'scene' && !current.project.graphData.nodes.some((existing) => existing.id === node.id));
      if (added) setSceneId(added.id);
    });
  }
  async function requestQuote(sourceNodeId: string) {
    await perform(async () => {
      const source = canvasRef.current.nodes.find((node) => node.id === sourceNodeId);
      if (!source || ![CanvasNodeType.Text, CanvasNodeType.Video, CanvasNodeType.Config].includes(source.type as CanvasNodeType)) throw new Error('请选择文字、视频或生成节点。');
      if (source.type === CanvasNodeType.Config && nodeDramaTask(source).id !== 'clip') throw new Error('请在生成节点中选择视频片段功能。');
      const selectedModel = getSelectedModel(source, 'video');
      const problem = generationInputProblem(source, displayNodes, canvasRef.current.connections, selectedModel);
      if (problem) throw new Error(problem);
      const metadata = source.metadata;
      const prompt = rawPrompt(source), duration = Number(metadata?.seconds || 5), resolution = metadata?.vquality || '720P';
      const ratio = canvasVideoRatio(metadata, selectedModel);
      const mode = metadata?.videoSourceMode ?? 'text';
      if (selectedModel && selectedModel.id !== 'wan-3-0' && !selectedModel.available) throw new Error(selectedModel.unavailableReason || '所选视频模型尚未准备');
      const limits = selectedModel?.capabilities;
      if ((limits?.resolutions && !limits.resolutions.includes(resolution)) || duration < (limits?.minDuration ?? 0) || duration > (limits?.maxDuration ?? Infinity)) {
        throw new Error('请为所选模型调整清晰度和时长');
      }
      const output = nextOutput(source, CanvasNodeType.Video);
      output.title = '视频结果'; output.metadata = { ...output.metadata, pendingResult: true };
      prepareOutput(source, output, { seconds: String(duration), vquality: resolution, videoRatio: ratio });
      await saveCanvas();
      const scene = envelopeRef.current?.project.graphData.nodes.find((node) => node.id === sceneId);
      const clipIds = new Set(scene?.data.timeline?.tracks.flatMap((track) => track.clips.map((clip) => clip.id)));
      const boundClip = tasks.find((task) => task.sceneId === sceneId && task.sourceNodeId === sourceNodeId && clipIds.has(task.clipId));
      const mediaOptions = mode === 'frames' ? { firstFrameNodeId: metadata?.firstFrameNodeId, lastFrameNodeId: metadata?.lastFrameNodeId }
        : mode === 'text' ? { videoInputMode: mode } : {
          referenceVideoNodeId: metadata?.referenceVideoNodeId,
          referenceImageNodeIds: limits?.maxReferenceImages ? videoReferenceImageIds(metadata, flattenNodeInputs(buildNodeGenerationInputs(source.id, displayNodes, canvasRef.current.connections))) : undefined,
          videoInputMode: mode,
        };
      const { generation } = await quoteGeneration(projectId, { sceneId, sourceNodeId, outputNodeId: output.id, clipId: boundClip?.clipId, prompt, duration, resolution, ratio, ...mediaOptions, modelProfileId: selectedModel?.id, operationId: operationId() });
      selectNode(sourceNodeId);
      setTasks((current) => [generation, ...current.filter((item) => item.id !== generation.id)]);
      focusNode(sourceNodeId);
      setReviewTaskId(generation.id);
    }, 'video');
  }
  async function generateFromConfig(sourceNodeId: string, translate = false) {
    const source = canvasRef.current.nodes.find((node) => node.id === sourceNodeId);
    if (!source || (source.type !== CanvasNodeType.Config && !(source.type === CanvasNodeType.Text && source.metadata?.generationMode === 'text'))) return;
    const kind = nodeDramaTask(source);
    if (kind.id === 'clip') { await requestQuote(source.id); return; }
    await perform(async () => {
      const selectedModel = getSelectedModel(source, kind.kind);
      if (kind.kind === 'text' && !(selectedModel?.available ?? textSettings?.configured)) throw new Error(selectedModel?.unavailableReason || '请由管理员配置文字生成服务。');
      if (kind.kind === 'text' && !textSettings?.canManage && !textSettings?.billingPolicyConfigured) throw new Error('平台销售计费规则尚未配置，文字生成尚未开放。');
      const problem = generationInputProblem(source, displayNodes, canvasRef.current.connections, selectedModel);
      if (problem && !translate) throw new Error(problem);
      const size = kind.kind === 'image' ? canvasImageSize(source.metadata, selectedModel) : undefined;
      if (kind.kind === 'image' && (!selectedModel?.available || !size)) throw new Error(selectedModel?.unavailableReason || '请配置可用的图片模型与尺寸。');
      const output = nextOutput(source, kind.kind === 'text' ? CanvasNodeType.Text : kind.kind === 'image' ? CanvasNodeType.Image : CanvasNodeType.Video);
      output.title = `${kind.label}结果`; output.metadata = { ...output.metadata, pendingResult: true };
      prepareOutput(source, output, size ? { imageWidth: size.imageWidth, imageHeight: size.imageHeight } : undefined);
      const expectedRevision = await saveCanvas();
      if (kind.kind === 'image' && selectedModel && size) {
        const job = await createCanvasImageJob(projectId, { sceneId, sourceNodeId, outputNodeId: output.id, prompt: rawPrompt(source), width: size.imageWidth, height: size.imageHeight, expectedRevision, modelProfileId: selectedModel.id, operationId: operationId() });
        setImageTasks((current) => [job, ...current.filter((task) => task.id !== job.id)]);
        setMessage('请在生成节点下方核对图片规格并确认。');
      } else if (kind.kind === 'text') {
        const { textGeneration } = await createTextGeneration(projectId, { sceneId, sourceNodeId, outputNodeId: output.id, task: translate ? 'translate' : kind.id, prompt: rawPrompt(source), expectedRevision, modelProfileId: selectedModel?.id, operationId: operationId() });
        setTextTasks((current) => [textGeneration, ...current.filter((task) => task.id !== textGeneration.id)]);
        setMessage('文字任务已保存，完成后会显示在当前卡片。');
      } else {
        const { generation } = await joinGeneration(projectId, { sceneId, sourceNodeId, outputNodeId: output.id, expectedRevision, operationId: operationId() });
        setTasks((current) => [generation, ...current.filter((task) => task.id !== generation.id)]);
        setMessage('视频拼接任务已保存，由本机顺序处理。');
      }
      focusNode(source.id);
    }, kind.kind);
  }
  async function confirmImage(task: CanvasImageJob) {
    await perform(async () => {
      const current = envelopeRef.current;
      if (!current || inputDirty) throw new Error('画布内容已有修改，请重新核对生成内容。');
      const job = await confirmCanvasImageJob(projectId, task.id, { expectedRevision: current.revision, operationId: operationId(), accountUsageConfirmed: true });
      setImageTasks((tasks) => tasks.map((item) => item.id === job.id ? job : item));
      setMessage('图片任务已提交，完成后会显示在当前卡片。');
    }, 'image');
  }
  function nextOutput(source: CanvasNodeData, type: CanvasNodeType) {
    const previous = canvasRef.current.nodes.find((node) => node.id === source.metadata?.outputNodeId && node.type === type && !node.metadata?.content && !node.metadata?.storageKey);
    const occupied = previous && (tasks.some((task) => task.outputNodeId === previous.id) || textTasks.some((task) => task.outputNodeId === previous.id) || imageTasks.some((task) => task.outputNodeId === previous.id));
    if (previous && !occupied) return structuredClone(previous);
    return canvasGeneratorOutputKind(source) ? makeNode(type, { ...source.position }) : placeResult(source, canvasRef.current.nodes, type);
  }
  function prepareOutput(source: CanvasNodeData, output: CanvasNodeData, patch: Partial<CanvasNodeMetadata> = {}) {
    const inline = canvasGeneratorOutputKind(source);
    if (inline) {
      output.metadata = { ...output.metadata, inlineResultOf: source.id };
      setPreviewedResultIds(current => ({ ...current, [source.id]: '' }));
    }
    changeCanvas((value) => ({ ...value,
      nodes: [...value.nodes.filter((node) => node.id !== output.id).map((node) => node.id === source.id ? { ...node, metadata: { ...node.metadata, ...patch, outputNodeId: output.id } } : node), output],
      connections: value.connections.some((edge) => edge.fromNodeId === source.id && edge.toNodeId === output.id) ? value.connections : [...value.connections, { id: operationId(), fromNodeId: source.id, toNodeId: output.id }],
    }));
  }
  function expandPlot(task: TextGenerationView) {
    if (!task.result || !('shots' in task.result)) return;
    const anchor = canvasRef.current.nodes.find((node) => node.id === task.outputNodeId && node.type === CanvasNodeType.Text);
    if (!anchor) { setError('原文字结果节点已移除，请先核对画布。'); return; }
    if (canvasRef.current.nodes.some((node) => node.metadata?.plotSpawnOf === anchor.id)) {
      setMessage('本次分镜流程已展开，可继续编辑已有节点。');
      return;
    }
    const referenceIds = displayNodes.filter((node) => node.type === CanvasNodeType.Image && node.metadata?.storageKey && !node.metadata?.plotSpawnOf && ['角色', '场景', '道具'].some((label) => node.title.includes(label))).map((node) => node.id);
    const occupiedBottom = Math.max(anchor.position.y, ...canvasRef.current.nodes.filter((node) => node.id !== anchor.id).map((node) => node.position.y + node.height + 80));
    const graph = buildShotFrameGraph({ ...anchor, position: { ...anchor.position, y: occupiedBottom } }, task.result.shots, referenceIds);
    const existingIds = new Set(canvasRef.current.nodes.map((node) => node.id));
    const added = graph.nodes.filter((node) => !existingIds.has(node.id));
    if (!added.length) { setMessage('本次分镜流程已展开，现有节点和编辑继续保留。'); return; }
    const existingEdges = new Set(canvasRef.current.connections.map((edge) => `${edge.fromNodeId}:${edge.toNodeId}`));
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, ...added], connections: [...value.connections, ...graph.connections.filter((edge) => !existingEdges.has(`${edge.fromNodeId}:${edge.toNodeId}`))] }));
    selectNode(added[0].id); setMessage(`已展开 ${task.result.shots.length} 镜、${graph.videoCount} 段视频，按当前每段5秒计算共${graph.totalSeconds}秒。可在生成节点中选择模型并逐个制作。`);
  }
  async function refreshSavedText(targetSceneId: string) {
    const next = await getCloudProject(projectId);
    if (!mergeServerCanvas(next, targetSceneId)) throw new Error('当前场景已经变化，请重新核对文字结果。');
  }
  async function applyText(task: TextGenerationView) {
    await perform(async () => {
      const targetSceneId = canvasRef.current.sceneId;
      if (dirtyRef.current) await saveCanvas();
      const current = envelopeRef.current; if (!current) throw new Error('项目尚未加载。');
      const result = await applyTextGeneration(task.id, current.revision);
      setTextTasks((values) => values.map((item) => item.id === task.id ? result.textGeneration : item));
      await refreshSavedText(targetSceneId); setMessage(dirtyRef.current ? '文字结果已保存，请求期间新增的编辑继续保留。' : '文字结果已保存到卡片。');
    }, 'text');
  }
  async function controlText(task: TextGenerationView, action: 'stop' | 'check') {
    await perform(async () => {
      const result = await (action === 'stop' ? stopTextGeneration(task.id) : getTextGeneration(task.id));
      setTextTasks((values) => values.map((item) => item.id === task.id ? result.textGeneration : item));
    }, 'text');
  }
  async function splitImage(params: ImageSplitParams) {
    const source = splitNode; if (!source) return;
    await perform(async () => {
      const expectedRevision = await saveCanvas();
      const result = await splitCloudImage(projectId, { ...params, sceneId, sourceNodeId: source.id, expectedRevision, operationId: operationId() });
      const saved = result.canvases.find((item) => item.sceneId === sceneId);
      if (!saved) throw new Error('服务端没有返回切片后的场景画布。');
      mergeServerCanvas(result, sceneId); setSplitNode(null); setMessage(`已保存 ${result.pieces.length} 张图片切片。`);
    });
  }
  async function confirm(task: GenerationView) {
    await perform(async () => {
      if (inputDirty) throw new Error('画布内容或连线已修改，请在节点内保存输入并重新报价。');
      const current = envelopeRef.current; if (!current) throw new Error('项目尚未加载。');
      const { generation } = await confirmGeneration(task, current.revision);
      setTasks((values) => values.map((item) => item.id === task.id ? generation : item));
      setReviewTaskId(null);
      setMessage('已确认，任务将按顺序执行。');
    }, 'video');
  }
  async function adopt(task: GenerationView) {
    await perform(async () => {
      if (dirty) await saveCanvas();
      const current = envelopeRef.current; if (!current) throw new Error('项目尚未加载。');
      const result = await adoptGeneration(task.id, current.revision); acceptEnvelope(result);
      const session = useProjectSessionStore.getState(); if (session.projectId === projectId && !session.dirty) session.loadProjectSnapshot(result.project);
      setMessage('视频已采用到任务绑定的场景与片段。');
    }, 'video');
  }
  async function resume(task: GenerationView) {
    await perform(async () => {
      const result = await resumeGeneration(task.id);
      setTasks((values) => values.map((item) => item.id === task.id ? result.generation : item));
    }, 'video');
  }
  async function upload(files: FileList | null) {
    const file = files?.[0]; if (!file) return;
    const referenceId = mediaReferenceTarget.current;
    await perform(async () => {
      if (dirty) await saveCanvas();
      const asset = await uploadCloudAsset(projectId, file); acceptEnvelope(await getCloudProject(projectId));
      const type = asset.type === 'image' ? CanvasNodeType.Image : asset.type === 'video' ? CanvasNodeType.Video : asset.type === 'audio' ? CanvasNodeType.Audio : CanvasNodeType.Text;
      const target = canvasRef.current.nodes.find((node) => node.id === uploadTarget.current && node.type === type);
      const node = target || makeNode(type, pickerPosition.current || { x: 60 + canvasRef.current.nodes.length * 30, y: 70 + canvasRef.current.nodes.length * 20 });
      const updated = applyNodeAsset(node, asset, Boolean(target));
      if (referenceId) addMediaReference(updated, referenceId);
      else {
        changeCanvas((value) => ({ ...value, nodes: target ? value.nodes.map((item) => item.id === target.id ? updated : item) : [...value.nodes, updated] }));
        selectNode(node.id);
      }
    });
    mediaReferenceTarget.current = null; uploadTarget.current = null; pickerPosition.current = undefined; if (fileInputRef.current) fileInputRef.current.value = '';
  }
  function openAssetUpload(position?: Position, referenceId?: string) {
    mediaReferenceTarget.current = referenceId ?? null;
    uploadTarget.current = null;
    pickerPosition.current = position;
    setRailMenu(null);
    setCreatePosition(null);
    fileInputRef.current?.click();
  }
  function openAssetLibrary(mode: 'assets' | 'history', position?: Position, referenceId?: string) {
    setLibraryError('');
    mediaReferenceTarget.current = referenceId ?? null;
    pickerPosition.current = position;
    setRailMenu(null);
    setCreatePosition(null);
    setLibraryMode(mode);
  }
  function chooseMediaReference(id: string, method: 'upload' | 'library') {
    const node = canvasRef.current.nodes.find((item) => item.id === id);
    if (!node) return;
    const position = { x: node.position.x - 380, y: node.position.y };
    if (method === 'upload') openAssetUpload(position, id);
    else openAssetLibrary('assets', position, id);
  }
  function addMediaReference(node: CanvasNodeData, targetId: string) {
    const nodes = [...canvasRef.current.nodes, node];
    const edge = resolveCanvasLink({ nodeId: node.id, handleType: 'source' }, targetId, nodes);
    if (!edge) { const reason = '当前节点无法接收这种参考素材。'; setLibraryError(reason); setError(reason); return false; }
    changeCanvas((value) => ({
      ...value,
      nodes: [...value.nodes.map((item) => item.id === targetId ? withLinkedVideoInputs(item, [node], nodes) : item), node],
      connections: [...value.connections, { ...edge, id: operationId() }],
    }));
    selectNode(targetId);
    return true;
  }
  function addProjectAsset(asset: SceneTurnAsset) {
    const origin = pickerPosition.current || screenToCanvas((containerRef.current?.getBoundingClientRect().left || 0) + 128, (containerRef.current?.getBoundingClientRect().top || 0) + 96);
    const node = applyNodeAsset(makeNode(asset.type, origin), asset);
    const referenceId = mediaReferenceTarget.current;
    if (referenceId) {
      if (!addMediaReference(node, referenceId)) return;
    } else changeCanvas((value) => ({ ...value, nodes: [...value.nodes, node] }));
    setLibraryMode(null);
    mediaReferenceTarget.current = null;
    pickerPosition.current = undefined;
    if (!referenceId) focusNode(node.id);
    return node;
  }

  const sceneTasks = useMemo(() => tasks.filter((task) => task.sceneId === sceneId), [tasks, sceneId]);
  const reviewTask = sceneTasks.find((task) => task.id === reviewTaskId && !task.material && task.status === 'awaiting_confirmation');
  const sceneImageTasks = imageTasks.filter((task) => task.sceneId === sceneId);
  const sceneTextTasks = textTasks.filter((task) => task.sceneId === sceneId);
  const generationReceipts = [...sceneTasks.filter(task => !task.material), ...sceneImageTasks, ...sceneTextTasks];
  const readyOwners = new Map(generationReceipts.filter(receipt => receipt.status === 'ready' && receipt.outputNodeId)
    .map(receipt => [receipt.outputNodeId, receipt.sourceNodeId]));
  const displayNodes = canvas.nodes.filter((node) => node.metadata?.agentWorkspace !== 'project').map((node) => {
    const projected = projectCanvasNode(node, sceneTasks);
    const owner = readyOwners.get(node.id);
    const result = owner && owner !== node.id && !projected.metadata?.inlineResultOf && !projected.metadata?.outputNodeId
      ? { ...projected, metadata: { ...projected.metadata, inlineResultOf: owner } } : projected;
    return embedded ? projectLegacyMaterialNode(result) : result;
  });
  const { nodes: visibleNodes, connections: visibleConnections, outputsBySourceId, visibleNodeIds, connectionIdsByVisibleId } =
    projectCanvasGenerationOutputs(displayNodes, canvas.connections, generationReceipts);
  visibleNodeIdsRef.current = visibleNodeIds;
  function inlinePreview(nodeId: string) {
    const group = outputsBySourceId.get(nodeId);
    if (!group) return undefined;
    const versions = group.versions.filter(hasGenerationContent);
    return versions.find(node => node.id === previewedResultIds[nodeId]) ?? versions[0] ?? group.current;
  }
  const scenes = envelope?.project.graphData.nodes.filter((node) => node.type === 'scene') || [];
  const agentBranches = useMemo(() => upstreamBranchEdges(envelope?.project.graphData.edges || [], sceneId), [envelope?.project.graphData.edges, sceneId]);
  const sceneTitle = (id: string) => { const scene = scenes.find((item) => item.id === id); return scene && 'title' in scene.data ? scene.data.title || '未命名场景' : '剧情场景'; };
  const selected = displayNodes.find((node) => node.id === selectedId);
  const selectedResult = selected && (inlinePreview(selected.id) ?? selected);
  const scriptTableNode = displayNodes.find((node) => node.id === scriptTableId);
  const productionNode = displayNodes.find(node => node.id === productionEditorId);
  function productionSources(id: string) {
    const nodes = canvas.connections.filter(e => e.toNodeId === id).flatMap(e => displayNodes.filter(n=>n.id===e.fromNodeId)).flatMap(n=>n.type===CanvasNodeType.Group?getGroupResourceNodes(n.id,displayNodes,canvas.connections):[n])
      .map(node => resolveCanvasGenerationOutput(node, displayNodes, canvas.connections));
    return [...new Map(nodes.map(n=>[n.id,n])).values()];
  }
  async function saveProductionFile(source: CanvasNodeData, file: File, anchor = source) {
    if (busyRef.current) throw new Error('正在保存其他素材。');
    if (!canvasRef.current.nodes.some(n=>n.id===source.id)) throw new Error('当前场景中的来源节点已关闭。');
    busyRef.current = true; setBusy(true);
    try {
      if (dirtyRef.current) await saveCanvas();
      const asset = await uploadCloudAsset(projectId,file);
      acceptEnvelope(await getCloudProject(projectId));
      const type = asset.type==='image'?CanvasNodeType.Image:asset.type==='video'?CanvasNodeType.Video:CanvasNodeType.Audio;
      const result = applyNodeAsset(placeResult(anchor,canvasRef.current.nodes.filter(node => !visibleNodeIdsRef.current.has(node.id)),type),asset);
      changeCanvas(c=>({...c,nodes:[...c.nodes.map(n=>n.id===source.id && n.metadata?.audioSettings?.job ? {...n,metadata:{...n.metadata,audioSettings:{...n.metadata.audioSettings,job:undefined}}}:n),result],connections:[...c.connections,{id:operationId(),fromNodeId:source.id,toNodeId:result.id}]}));
      await saveCanvas();
      setMessage('已保存到素材画布');
    } finally { busyRef.current=false;setBusy(false); }
  }
  function editAudio(source: CanvasNodeData) {
    const next=placeResult(source,canvasRef.current.nodes,CanvasNodeType.Config);
    next.title='音频编辑';next.metadata={productionTool:'audio-edit'};next.width=320;next.height=146;
    changeCanvas(c=>({...c,nodes:[...c.nodes,next],connections:[...c.connections,{id:operationId(),fromNodeId:source.id,toNodeId:next.id}]}));
    focusNode(next.id);setProductionEditorId(next.id);
  }
  const generatedAssetIds = new Set([
    ...(envelope?.project.assets ?? []).filter(asset => asset.metadata?.imageJobId || asset.metadata?.generationId || asset.metadata?.provider || asset.metadata?.codexImageJobId).map(asset => asset.id),
    ...tasks.flatMap((task) => task.status === 'ready' && task.assetId ? [task.assetId] : []),
    ...imageTasks.flatMap((task) => task.status === 'ready' && task.assetId ? [task.assetId] : []),
  ]);
  const availableAssets = (envelope?.project.assets ?? []).filter((asset) => libraryMode !== 'history' || generatedAssetIds.has(asset.id));
  function updateScriptTable(id: string, rows: CanvasScriptRow[]) {
    const content = rows.map(formatShot).join('\n\n');
    changeCanvas((value) => ({ ...value, nodes: value.nodes.map((node) => node.id === id ? { ...node, type: CanvasNodeType.Text, ...(!node.metadata?.scriptRows ? { width: 800, height: 400 } : {}), metadata: { ...node.metadata, scriptRows: rows, content } } : node) }));
  }
  async function addScriptInputs(target: CanvasNodeData, sources: CanvasNodeData[], prompt?: string) {
    const current = canvasRef.current;
    if (!current.nodes.some((node) => node.id === target.id)) throw new Error('当前画布已切换。');
    const placed = placeScriptNodes(target, sources, current.nodes);
    const textTarget = target.type === CanvasNodeType.Text || target.metadata?.generationMode === 'text';
    const imageModel = textTarget && sources.some((node) => node.type === 'image') && !target.metadata?.modelProfileId && !getSelectedModel(target, 'text')?.capabilities.maxReferences
      ? providerCatalog?.models.find((model) => model.kind === 'text' && model.capabilities.maxReferences)?.id : undefined;
    changeCanvas({ ...current, nodes: [...current.nodes.map((node) => node.id === target.id ? {
      ...node, metadata: { ...node.metadata, ...(prompt ? { composerContent: node.metadata?.composerContent?.trim() || prompt } : {}),
        ...(imageModel ? { modelProfileId: imageModel } : {}),
        ...(node.type === CanvasNodeType.Text ? { generationMode: 'text' as const, dramaTask: 'text' } : {}) },
    } : node), ...placed], connections: [...current.connections, ...placed.map((node) => linkNodes(node.id, target.id))] });
    focusNode(target.id);
  }
  async function scriptImageNodes(files: File[], project: string) {
    const nodes: CanvasNodeData[] = [];
    for (const file of files) {
      const asset = await uploadCloudAsset(project, file);
      if (asset.type !== 'image') throw new Error('脚本参考需要图片文件。');
      nodes.push(applyNodeAsset(makeNode('image', { x: 0, y: 0 }), asset));
    }
    acceptEnvelope(await getCloudProject(project));
    return nodes;
  }
  async function applyMediaPreset(node: CanvasNodeData, mode: 'frames' | 'first' | 'upscale') {
    await perform(async () => {
      if (dirtyRef.current) await saveCanvas();
      const samples = mode === 'upscale' ? [['upscale-sample', '图片高清参考图']] : mode === 'first' ? [['single-frame', '首帧']] : [['sparkling-first', '首帧'], ['sparkling-last', '尾帧']];
      const files: File[] = [];
      for (const [filename, label] of samples) {
        const response = await fetch('/sceneturn/canvas/' + filename + '.png');
        if (!response.ok) throw new Error('无法读取预设素材。');
        files.push(new File([await response.blob()], label + '.png', { type: 'image/png' }));
      }
      const sources = await scriptImageNodes(files, projectId);
      const prompt = mode === 'upscale' ? '提升参考图清晰度，保留原始构图、人物、材质和颜色，恢复自然细节。' : mode === 'first' ? '以当前图为首帧生成视频。' : '手从白帆布包中取出青柠气泡水罐的特写，过渡到短发女生在户外草地闭眼仰头喝同款气泡水的中景，日系清新治愈风格，明亮自然光，柔和暖调，胶片质感，动作流畅自然。';
      await addScriptInputs(node, sources, prompt);
      if (mode === 'upscale') changeNode(node.id, { title: '图片高清' });
      else {
        changeMetadata(node.id, { videoSourceMode: 'frames', firstFrameNodeId: sources[0].id, lastFrameNodeId: sources[1]?.id });
        changeCanvas(current => {
          const parent = current.nodes.find(item => item.type === CanvasNodeType.Group && item.id === node.metadata?.groupId);
          const members = new Set([node.id, ...sources.map(item => item.id)]);
          const bounds = getGroupWrapRect(current.nodes.filter(item => members.has(item.id) || (parent && item.metadata?.groupId === parent.id)));
          const group = { ...(parent ?? makeNode(CanvasNodeType.Group, bounds)), position: { x: bounds.x, y: bounds.y }, width: bounds.width, height: bounds.height };
          if (!parent) group.title = mode === 'frames' ? '预设 - 首尾帧生成视频' : '预设 - 首帧生成视频';
          const nodes = current.nodes.map(item => item.id === parent?.id ? group : members.has(item.id) ? { ...item, metadata: { ...item.metadata, groupId: group.id } } : item);
          return { ...current, nodes: parent ? nodes : [group, ...nodes] };
        });
      }
      await saveCanvas();
    });
  }
  async function applyScriptPreset(node: CanvasNodeData, mode: 'story' | 'characters' | 'reverse') {
    await perform(async () => {
      if (mode === 'story') {
        const text = nodeForMenu('text', { x: 0, y: 0 });
        text.metadata = { ...text.metadata, content: scriptExample };
        await addScriptInputs(node, [text], '根据参考剧本逐镜拆解人物、画面和动作。');
        return;
      }
      if (dirtyRef.current) await saveCanvas();
      const samples = mode === 'characters' ? [['character-one', '角色 1'], ['character-two', '角色 2']] : [['reverse-image', '示例图片']];
      const files: File[] = [];
      for (const [path, label] of samples) {
        const response = await fetch(`/sceneturn/canvas/${path}.png`);
        if (!response.ok) throw new Error(`参考图片读取失败（${response.status}）。`);
        files.push(new File([await response.blob()], `${label}.png`, { type: 'image/png' }));
      }
      await addScriptInputs(node, await scriptImageNodes(files, projectId), mode === 'characters' ? characterExample : reverseExample);
    });
  }
  function applyTextShortcut(source: CanvasNodeData, action: Exclude<TextShortcut, 'write'>) {
    if (action === 'reverse') { void applyScriptPreset(source, 'reverse'); return; }
    const type = action === 'video' ? 'video' : 'audio';
    const next = nodeForMenu(type, placeResult(source, canvasRef.current.nodes, type === 'video' ? CanvasNodeType.Video : CanvasNodeType.Audio).position);
    next.metadata = { ...next.metadata, composerContent: action === 'video' ? '根据文字描述生成视频。' : '根据文字描述生成音乐。', ...(action === 'music' ? { audioMode: 'music' } : {}) };
    changeCanvas((current) => ({ ...current, nodes: [...current.nodes, next], connections: [...current.connections, linkNodes(source.id, next.id)] }));
    focusNode(next.id);
  }
  const disconnectReference = (fromNodeId: string, toNodeId: string, edgeIds?: Set<string>) => {
    changeCanvas((value) => {
      const removed = value.connections.filter(edge => edgeIds ? edgeIds.has(edge.id) : edge.fromNodeId === fromNodeId && edge.toNodeId === toNodeId);
      const sourceIds = new Set(removed.map(edge => edge.fromNodeId));
      const targetIds = new Set(removed.map(edge => edge.toNodeId));
      for (const node of value.nodes) if (node.metadata?.groupId && sourceIds.has(node.metadata.groupId)) sourceIds.add(node.id);
      const removedIds = new Set(removed.map(edge => edge.id));
      const connections = value.connections.filter(edge => !removedIds.has(edge.id));
      const nodes = value.nodes.map((node) => targetIds.has(node.id) ? clearDisconnectedVideoReferences(node, sourceIds, value.nodes, connections) : node);
      return { ...value, nodes, connections };
    });
  };
  function createConfigForNode(node: CanvasNodeData) {
    const config = placeResult(node, canvasRef.current.nodes, CanvasNodeType.Config);
    config.metadata = { ...config.metadata, composerContent: rawPrompt(node), generationMode: 'video', dramaTask: 'clip', seconds: node.metadata?.seconds || '5', vquality: node.metadata?.vquality || '720P' };
    const references = canvasRef.current.connections.filter((edge) => edge.toNodeId === node.id).map((edge) => ({ ...edge, id: operationId(), toNodeId: config.id }));
    if (node.type === CanvasNodeType.Text) {
      config.metadata.composerContent = `根据 @[node:${node.id}] 生成视频。`;
      references.push({ id: operationId(), fromNodeId: node.id, toNodeId: config.id });
    }
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, config], connections: [...value.connections, ...references] })); focusNode(config.id);
  }
  function activateCanvasNode(id: string, additive: boolean) {
    if (referencePickerId) {
      if (id === referencePickerId) {
        setReferencePickerId(null);
        selectNode(id);
      } else {
        const edge = resolveCanvasLink({ nodeId: id, handleType: 'source' }, referencePickerId, displayNodes);
        if (edge) appendCanvasLink(edge);
      }
      return;
    }
    if (additive) {
      const next = new Set(selectedIds);
      if (next.has(id)) next.delete(id); else next.add(id);
      setSelectedIds(next);
      setSelectedId(next.has(id) ? id : next.values().next().value ?? null);
      return;
    }
    if (selectedId !== id || selectedIds.size !== 1) selectNode(id);
  }
  function deselectCanvas() {
    if (referencePickerId) {
      setReferencePickerId(null);
      selectNode(referencePickerId);
      return;
    }
    setSelectedId(null);
    setSelectedIds(new Set());
    setSelectedConnectionId(null);
    setCreatePosition(null);
    closeConnectionCreate();
  }
  function beginCanvasNodeDrag(id: string) {
    if (referencePickerId || loadingCanvas || busyRef.current) return;
    const nodes = canvasRef.current.nodes;
    const anchor = nodes.find(item => item.id === id);
    if (!anchor) return;
    const movingIds = selectedIds.has(id) ? selectedIds : new Set([id]);
    if (!selectedIds.has(id)) selectNode(id);
    const initialPositions = new Map(
      nodes.filter(node => movingIds.has(node.id) || (node.metadata?.groupId && movingIds.has(node.metadata.groupId))).map(node => [node.id, node.position]),
    );
    return (delta: Position) => {
      const dx = snap ? Math.round((anchor.position.x + delta.x) / 16) * 16 - anchor.position.x : delta.x;
      const dy = snap ? Math.round((anchor.position.y + delta.y) / 16) * 16 - anchor.position.y : delta.y;
      const currentNodes = canvasRef.current.nodes;
      const nextNodes = currentNodes.map(node => {
        const initial = initialPositions.get(node.id);
        if (!initial) return node;
        const position = { x: initial.x + dx, y: initial.y + dy };
        return position.x === node.position.x && position.y === node.position.y ? node : { ...node, position };
      });
      if (nextNodes.some((node, index) => node !== currentNodes[index])) changeCanvas(value => ({ ...value, nodes: nextNodes }));
    };
  }
  const regenerate = (id: string) => { const source = canvasRef.current.nodes.find((node) => node.id === id); if (source?.type === CanvasNodeType.Config) void generateFromConfig(id); else void requestQuote(id); };
  const retryNode = (node: CanvasNodeData) => { const task = sceneTasks.find((item) => item.outputNodeId === node.id || item.sourceNodeId === node.id); if (task && canResumeVideo(task)) void resume(task); else if (task?.status === 'failed' && !task.material) regenerate(task.sourceNodeId); else if (!task && (node.type === CanvasNodeType.Video || node.type === CanvasNodeType.Text)) createConfigForNode(node); };
  const renderNodePanel = (node: CanvasNodeData) => {
    if (node.metadata?.publicMaterialRole) return <p>在“问问 AI”中选择成果并提出修改。</p>;
    if (node.type===CanvasNodeType.Audio) return <AudioComposer key={node.id} projectId={projectId} node={node} references={productionSources(node.id)} onChange={p=>changeMetadata(node.id,p)} onUpload={()=>openAssetUpload(undefined,node.id)} onReference={()=>setReferencePickerId(node.id)} onEdit={()=>editAudio(node)} onSave={file=>saveProductionFile(node,file)} onCheckpoint={async()=>{await saveCanvas();}}/>;
    if (node.metadata?.productionTool==='director') return null;
    const cutPanel = node.metadata?.productionTool==='audio-edit' || node.type===CanvasNodeType.Config && node.metadata?.dramaTask==='join';
    const nodeTasks = sceneTasks.filter((task) => !task.material && (task.sourceNodeId === node.id || task.outputNodeId === node.id));
    const nodeTextTasks = textTasks.filter((task) => task.sceneId === sceneId && (task.sourceNodeId === node.id || task.outputNodeId === node.id));
    const nodeImageTasks = sceneImageTasks.filter((task) => task.sourceNodeId === node.id || task.outputNodeId === node.id);
    return <div data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()} onWheel={(event) => event.stopPropagation()}>
      {cutPanel ? <div className="prod-panel prod-cut-panel"><button onClick={()=>setReferencePickerId(node.id)}>参考</button><textarea aria-label="剪辑说明" placeholder="描述想剪成什么效果" value={node.metadata?.composerContent || ''} onChange={e=>changeMetadata(node.id,{composerContent:e.target.value})}/><button onClick={()=>setProductionEditorId(node.id)}>{node.metadata?.productionTool==='audio-edit'?'编辑音频':'素材混剪'}</button></div> : (node.type === CanvasNodeType.Config || node.type === CanvasNodeType.Text && node.metadata?.generationMode === 'text' && !node.metadata?.scriptRows || node.type === CanvasNodeType.Video && !node.metadata?.pendingResult && !nodeTasks.some((task) => task.outputNodeId === node.id)) && renderConfigPanel(node)}
      {nodeTextTasks.map((task) => <TextGenerationTaskCard key={task.id} task={task} inline={visibleNodeIds.has(task.outputNodeId)} busy={busy || loadingCanvas} onStop={(value) => { void controlText(value, 'stop'); }} onResume={(value) => { void controlText(value, 'check'); }} onApply={(value) => { void applyText(value); }} onExpandPlot={expandPlot} onFocusOutput={focusNode} />)}
      {nodeImageTasks.map((task) => <CanvasImageTaskCard key={task.id} task={task} name={providerCatalog?.models.find((item) => item.id === task.modelProfileId)?.label || task.model} busy={busy || loadingCanvas} canConfirm={!inputDirty && canvas.nodes.some((item) => item.id === task.sourceNodeId && item.metadata?.outputNodeId === task.outputNodeId) && canvas.nodes.some((item) => item.id === task.outputNodeId)} onConfirm={(value) => { void confirmImage(value); }} onFocus={focusNode} />)}
      {nodeTasks.map((task) => <GenerationTaskCard key={task.id} task={task} revision={envelope?.revision || 0} inputDirty={inputDirty} busy={busy || loadingCanvas} sourceAvailable={canvas.nodes.some((item) => item.id === task.sourceNodeId)} onReview={(id) => { setError(''); setReviewTaskId(id); }} onRequote={regenerate} onAdopt={(value) => { void adopt(value); }} onResume={(value) => { void resume(value); }} onFocusOutput={focusNode} />)}
    </div>;
  };
  function groupSelection() { const bounds = getGroupWrapRect(collectGroupMemberNodes(selectedIds, canvas.nodes)); const group = { ...makeNode(CanvasNodeType.Group, { x: bounds.x, y: bounds.y }), width: bounds.width, height: bounds.height }; const result = applyGroupSelection(selectedIds, canvas.nodes, canvas.connections, group); if (result) { changeCanvas((value) => ({ ...value, nodes: result.nodes, connections: result.connections })); selectNode(group.id); } }
  function ungroupSelection() { const result = applyUngroupSelection(selectedIds, canvas.nodes, canvas.connections); if (result) { changeCanvas((value) => ({ ...value, nodes: result.nodes, connections: result.connections })); setSelectedId(null); setSelectedIds(new Set(result.selectedIds)); } }
  function selectionForCopy() {
    const ids = new Set([...selectedIds, ...collectGroupMemberNodes(selectedIds, visibleNodes).map(node => node.id)]);
    const nodes = visibleNodes.filter(node => ids.has(node.id)).flatMap(node => {
      const result = inlinePreview(node.id);
      if (!result || !hasGenerationContent(result)) return [node];
      return [
        { ...node, metadata: { ...node.metadata, outputNodeId: result.id } },
        { ...result, position: node.position, metadata: { ...result.metadata, inlineResultOf: node.id, groupId: undefined } },
      ];
    });
    return { nodes, connections: canvas.connections };
  }
  function copySelection() {
    const { nodes, connections } = selectionForCopy();
    const ids = new Set(nodes.map(node => node.id));
    clipboard.current = { projectId, sceneId, nodes: structuredClone(nodes), connections: structuredClone(connections.filter(edge => ids.has(edge.fromNodeId) && ids.has(edge.toNodeId))) };
  }
  function duplicateSelection(paste = false) {
    const copied = paste ? clipboard.current : null;
    if (paste && (!copied || copied.projectId !== projectId || copied.sceneId !== sceneId)) return;
    const { nodes: originals, connections: originalConnections } = copied ?? selectionForCopy();
    if (!originals.length) return;
    const idMap = new Map(originals.map((node) => [node.id, operationId()]));
    const remapId = (id?: string) => id ? idMap.get(id) ?? (copied ? undefined : id) : undefined;
    const remapReferences = (value?: string) => value?.replace(/@\[node:([^\]]+)\]/g, (reference, id: string) => idMap.has(id) ? `@[node:${idMap.get(id)}]` : copied ? '' : reference);
    const copies = originals.map((node): CanvasNodeData => ({
      ...structuredClone(node), id: idMap.get(node.id)!, title: `${node.title} 副本`, position: { x: node.position.x + 36, y: node.position.y + 36 },
      metadata: {
        ...structuredClone(node.metadata), publicMaterialRole: undefined, creativeArtifact: undefined,
        composerContent: remapReferences(node.metadata?.composerContent), prompt: remapReferences(node.metadata?.prompt),
        agentGoal: remapReferences(node.metadata?.agentGoal), agentTargetIds: node.metadata?.agentTargetIds?.map((id) => idMap.get(id) ?? id),
        groupId: node.metadata?.groupId ? idMap.get(node.metadata.groupId) : undefined,
        outputNodeId: node.metadata?.outputNodeId ? idMap.get(node.metadata.outputNodeId) : undefined,
        inlineResultOf: node.metadata?.inlineResultOf ? idMap.get(node.metadata.inlineResultOf) : undefined,
        firstFrameNodeId: remapId(node.metadata?.firstFrameNodeId), lastFrameNodeId: remapId(node.metadata?.lastFrameNodeId),
        referenceVideoNodeId: remapId(node.metadata?.referenceVideoNodeId),
        videoReferenceImageNodeIds: node.metadata?.videoReferenceImageNodeIds?.flatMap(id => remapId(id) ?? []),
        status: node.metadata?.storageKey || node.metadata?.content ? 'success' : 'idle',
        pendingResult: false, textGenerationId: undefined, imageGenerationId: undefined, videoTaskId: undefined, videoTaskProvider: undefined, errorDetails: undefined,
      },
    }));
    const connections = originalConnections.filter((edge) => idMap.has(edge.toNodeId)).map((edge) => ({ ...edge, id: operationId(), fromNodeId: idMap.get(edge.fromNodeId) || edge.fromNodeId, toNodeId: idMap.get(edge.toNodeId)! }));
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, ...copies], connections: [...value.connections, ...connections] }));
    const visibleCopies = copies.filter(node => !node.metadata?.inlineResultOf);
    setSelectedIds(new Set(visibleCopies.map((node) => node.id))); setSelectedId(selectedId ? idMap.get(selectedId) || visibleCopies[0]?.id || null : visibleCopies[0]?.id || null); setSelectedConnectionId(null);
  }
  function setBatchPrimary(nodeId: string, itemId: string) {
    const node = canvasRef.current.nodes.find((item) => item.id === nodeId);
    const image = node?.metadata?.images?.find((item) => item.id === itemId);
    const text = node?.metadata?.texts?.find((item) => item.id === itemId);
    if (image?.content) changeMetadata(nodeId, { primaryImageId: image.id, content: image.content, storageKey: image.storageKey, naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight });
    else if (text?.content) changeMetadata(nodeId, { primaryTextId: text.id, content: text.content });
  }
  function downloadImage(node: CanvasNodeData, imageId: string) {
    const image = node.metadata?.images?.find((item) => item.id === imageId);
    if (!image?.content) return;
    const anchor = document.createElement('a'); anchor.href = image.storageKey ? assetContentUrl(image.storageKey) : image.content; anchor.download = node.title; anchor.click();
  }
  function duplicateImage(node: CanvasNodeData, imageId: string) {
    const image = node.metadata?.images?.find((item) => item.id === imageId);
    if (!image?.content) return;
    const copy = makeNode(CanvasNodeType.Image, { x: node.position.x + node.width + 40, y: node.position.y });
    copy.metadata = { content: image.content, storageKey: image.storageKey, naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight, mimeType: image.mimeType, bytes: image.bytes, status: 'success' };
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, copy] })); selectNode(copy.id);
  }
  function getSelectedModel(node: CanvasNodeData, kind: 'text' | 'image' | 'video') {
    const available = providerCatalog?.models.filter((item) => item.kind === kind) ?? [];
    return available.find((item) => item.id === node.metadata?.modelProfileId) ?? available.find((item) => item.id === providerCatalog?.defaults[kind]);
  }
  function renderConfigPanel(node: CanvasNodeData) {
    const inputs = buildNodeGenerationInputs(node.id, displayNodes, canvas.connections);
    const connectedNodes = displayNodes.filter((item) => canvas.connections.some((edge) => edge.fromNodeId === item.id && edge.toNodeId === node.id))
      .map((source) => resolveCanvasGenerationOutput(source, displayNodes, canvas.connections));
    const joinedVideos = [...new Map(connectedNodes.flatMap((item) => item.type === CanvasNodeType.Group ? getGroupResourceNodes(item.id, displayNodes, canvas.connections) : [item]).filter(item => item.type === CanvasNodeType.Video).map((item) => [item.id, item])).values()];
    let joinedSeconds = 0, estimated = false, unknown = false;
    for (const video of joinedVideos) {
      const measured = mediaDurations[video.id];
      if (measured && measured.src === video.metadata?.content) { joinedSeconds += measured.seconds; continue; }
      if (video.metadata?.durationMs) { joinedSeconds += video.metadata.durationMs / 1000; continue; }
      const config = canvas.nodes.find((item) => item.type === CanvasNodeType.Config && nodeDramaTask(item).id === 'clip' && canvas.connections.some((edge) => edge.fromNodeId === item.id && edge.toNodeId === video.id));
      if (config) { joinedSeconds += Number(config.metadata?.seconds || 5); estimated = true; } else unknown = true;
    }
    const kind = nodeDramaTask(node).kind;
    const selectedProfile = getSelectedModel(node, kind);
    const inputProblem = generationInputProblem(node, displayNodes, canvas.connections, selectedProfile);
    const defaultProfile = providerCatalog?.models.find((item) => item.id === providerCatalog.defaults[kind]);
    const textConnected = Boolean(textSettings && (selectedProfile?.available ?? textSettings.configured) && (textSettings.canManage || textSettings.billingPolicyConfigured));
    const textServiceLabel = !textSettings ? '正在读取文本模型配置…' : textConnected ? `${selectedProfile?.label ?? textSettings.model} · 可生成` : '文字模型尚未配置或尚未开放。';
    if (kind === 'text') {
      const links = canvas.connections.filter((edge) => edge.toNodeId === node.id);
      return <CanvasTextGenerator node={node} nodes={displayNodes} inputs={inputs}
        connected={links.flatMap((edge) => connectedNodes.filter((source) => source.id === edge.fromNodeId))}
        models={providerCatalog?.models.filter((item) => item.kind === kind) ?? []} model={selectedProfile}
        images={(envelope?.project.assets || []).filter((asset) => asset.type === 'image')} status={textServiceLabel} inputProblem={inputProblem} disabled={busy || loadingCanvas || !textConnected}
        onModel={(id) => changeMetadata(node.id, { modelProfileId: id })}
        onPrompt={(composerContent) => changeMetadata(node.id, { composerContent })}
        onChooseCanvas={() => setReferencePickerId(node.id)} onRemove={(from) => disconnectReference(from, node.id)}
        onReorder={(from, to) => {
          if (from === to) return;
          changeCanvas((current) => {
            const edges = [...current.connections];
            const a = edges.findIndex((edge) => edge.toNodeId === node.id && edge.fromNodeId === from);
            const b = edges.findIndex((edge) => edge.toNodeId === node.id && edge.fromNodeId === to);
            if (a < 0 || b < 0) return current;
            const [edge] = edges.splice(a, 1); edges.splice(b, 0, edge);
            return { ...current, connections: edges };
          });
        }}
        onUpload={(files) => void perform(async () => {
          if (files.some((file) => !['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type))) throw new Error('请选择 PNG、JPEG、WebP 或 GIF 图片。');
          if (dirtyRef.current) await saveCanvas();
          await addScriptInputs(node, await scriptImageNodes(files, projectId));
        })}
        onLibrary={(assets) => void addScriptInputs(node, assets.map((asset) => applyNodeAsset(makeNode('image', { x: 0, y: 0 }), asset)))}
        onGenerate={(translate) => { void generateFromConfig(node.id, translate); }} />;
    }
    return <CanvasConfigNodePanel ownerId={user!.id} onChooseMediaReference={chooseMediaReference} node={node} nodes={displayNodes} connections={canvas.connections} connectedNodes={connectedNodes} inputs={inputs} model={selectedProfile?.label ?? model} models={providerCatalog?.models.filter((item) => item.kind === kind) ?? []} selectedProfile={selectedProfile} defaultProfile={defaultProfile} textServiceLabel={textServiceLabel} textConnected={textConnected} busy={busy || loadingCanvas} canGenerate={!inputProblem} inputProblem={inputProblem} joinDuration={{ count: joinedVideos.length, seconds: unknown ? null : joinedSeconds, estimated }} onConfigChange={changeMetadata} onGenerate={(id) => { if (node.type === CanvasNodeType.Video) void requestQuote(id); else void generateFromConfig(id); }} onDisconnectReference={disconnectReference} onStartReferenceSelection={setReferencePickerId} />;
  }
  function renderGeneratorCard(node: CanvasNodeData) {
    if (node.metadata?.productionTool==='director') return <div className="fmv-libtv-generator-card prod-entry" data-canvas-no-zoom><Boxes size={32} strokeWidth={1.25}/><span>在3D空间中搭建场景并进行多视角截图</span><button type="button" onClick={()=>setProductionEditorId(node.id)}>打开导演台</button></div>;
    if (node.metadata?.productionTool==='audio-edit') return <div className="fmv-libtv-generator-card prod-entry" data-canvas-no-zoom><AudioLines size={28}/><button onClick={()=>setProductionEditorId(node.id)}>编辑音频</button></div>;
    if (node.metadata?.dramaTask==='join') {
      const sources=productionSources(node.id);
      return <div className="fmv-libtv-generator-card prod-entry prod-cut-entry" data-canvas-no-zoom><Scissors size={28} strokeWidth={1.25}/><span>{sources.length?'已连接 '+sources.length+' 项素材':'空空如也，请连接视频节点后操作'}</span><div><small>尝试：</small>{['讲解视频','批量广告','口播视频'].map(label=><button key={label} disabled title="当前项目尚未配置此模式">{label}</button>)}<button disabled={!sources.some(n=>n.type==='video'||n.type==='audio')} onClick={()=>setProductionEditorId(node.id)}>素材混剪</button></div></div>;
    }
    const task = nodeDramaTask(node);
    if (task.id === 'plot' || node.metadata?.scriptRows || node.metadata?.scriptVersion === 'legacy' || node.title === '脚本（旧版）') {
      const editScript = () => {
        if (!node.metadata?.scriptRows) updateScriptTable(node.id, [createCanvasScriptRow({ duration: 5 })]);
        setScriptTableId(node.id);
      };
      if (node.metadata?.scriptRows) return <div className="fmv-script-preview" data-canvas-no-zoom>
        <div><table><thead><tr>{['镜号','时长','画面描述','景别','光影氛围','音效','对白','最终提示词'].map((heading) => <th key={heading}>{heading}</th>)}</tr></thead>
          <tbody>{node.metadata.scriptRows.map((row: CanvasScriptRow, index) => <tr key={row.id}>{[index + 1, row.duration, row.visual || row.title, row.shotSize, row.lighting, row.sound, row.dialogue, row.finalPrompt].map((value, column) => <td key={column}>{value || '–'}</td>)}</tr>)}</tbody></table></div>
        <button type="button" onClick={editScript}>打开脚本节点 →</button>
      </div>;
      return <div className="fmv-text-shortcuts" data-canvas-no-zoom>
        <NotebookText className="fmv-text-shortcuts-symbol" aria-hidden="true" />
        <div className="fmv-text-shortcuts-actions"><span>尝试：</span>
          <button type="button" disabled={busy} onClick={() => void applyScriptPreset(node, 'story')}><Type />剧本生成分镜脚本</button>
          <button type="button" disabled={busy} onClick={() => void applyScriptPreset(node, 'characters')}><ImageIcon />角色生成分镜脚本</button>
          <button type="button" onClick={editScript}><NotebookText />自己编写分镜脚本</button>
        </div>
      </div>;
    }
    if ((task.kind === 'image' || task.id === 'clip') && !rawPrompt(node).trim() && !canvas.connections.some((edge) => edge.toNodeId === node.id)) {
      return <div className="fmv-libtv-generator-card fmv-media-idle" data-testid="generation-card-preview">
        {task.kind === 'image' ? <ImageIcon size={42} /> : <Film size={36} />}
        <div><small>尝试：</small>{task.kind === 'image' ? <>
          <button onClick={() => chooseMediaReference(node.id, 'upload')}>图生图</button>
          <button disabled={busy} onClick={() => void applyMediaPreset(node, 'upscale')}>图片高清</button></> : <>
          <button disabled={busy} onClick={() => void applyMediaPreset(node, 'frames')}>首尾帧生成视频</button>
          <button disabled={busy} onClick={() => void applyMediaPreset(node, 'first')}>首帧生成视频</button></>}</div>
      </div>;
    }
    const Icon = task.id === 'join' ? Scissors : task.kind === 'video' ? Film : task.kind === 'image' ? ImageIcon : Type;
    return <div className="fmv-libtv-generator-card" data-testid="generation-card-preview">
      <Icon size={28} strokeWidth={1.25} aria-hidden="true" />
      {node.metadata?.status === 'loading' ? '正在生成' : task.id === 'join' ? '空空如也，请连接视频节点后操作' : null}
    </div>;
  }
  function draftMediaEdit(source: CanvasNodeData, name: string, prompt: string, anchor = source) {
    const video = source.type === 'video';
    const draft = nodeForMenu(video ? 'video' : 'image', placeResult(anchor, canvasRef.current.nodes.filter(node => !visibleNodeIdsRef.current.has(node.id))).position);
    draft.title = name;
    const supported = providerCatalog?.models.filter((item) => item.kind === 'video' && item.capabilities.videoInputModes?.includes('edit'));
    const model = supported?.find((item) => item.available) ?? supported?.[0];
    draft.metadata = { ...draft.metadata, composerContent: prompt, ...(video ? { videoSourceMode:'edit', referenceVideoNodeId:source.id, modelProfileId:model?.id } : {}) };
    changeCanvas((value) => ({ ...value, nodes:[...value.nodes,draft], connections:[...value.connections,linkNodes(source.id,draft.id)] }));
    focusNode(draft.id);
  }
  function renderCanvasNode(node: CanvasNodeData) {
    if (node.type === 'agent') return <AgentCanvasNode key={node.id} node={node} nodes={canvas.nodes} projectId={projectId} sceneId={sceneId}
      branchOptions={agentBranches.map((edge) => ({ id: edge.id, title: `${sceneTitle(edge.source)} → ${sceneTitle(edge.target)}` }))}
      selected={selectedIds.has(node.id)} disabled={busy || loadingCanvas} onSelect={(event, id) => {
        if (event.button === 0 && !referencePickerId) activateCanvasNode(id, event.shiftKey || event.metaKey || event.ctrlKey);
      }} onChange={changeMetadata} onDelete={(id) => removeNodes([id])}
      beforeRequest={async () => { if (dirtyRef.current) return saveCanvas(); const current = envelopeRef.current; if (!current) throw new Error('请先读取当前项目。'); return current.revision; }}
      refreshProject={async () => { await refreshSavedText(sceneId); }} onFocusSource={(id) => { if (canvasRef.current.nodes.some((item) => item.id === id)) focusNode(id); else if (scenes.some((item) => item.id === id)) void selectScene(id); }} />;
    const output = outputsBySourceId.get(node.id);
    const taskNodeId = output?.current.id ?? node.id;
    const task = sceneTasks.find((item) => item.outputNodeId === taskNodeId || (!item.outputNodeId && item.sourceNodeId === node.id && node.type === CanvasNodeType.Video));
    const imageTask = sceneImageTasks.find((item) => item.outputNodeId === taskNodeId);
    const textTask = output && sceneTextTasks.find((item) => item.outputNodeId === taskNodeId);
    const statusLabel = task ? taskLabels[task.status] : imageTask ? canvasImageStatus[imageTask.status] : textTask ? textGenerationStatus[textTask.status] : undefined;
    const preview = inlinePreview(node.id);
    const working = imageTask && ['queued', 'claimed', 'submitting', 'response_saved'].includes(imageTask.status) ||
      textTask && ['queued', 'starting', 'submitting', 'running'].includes(textTask.status);
    const failed = imageTask && ['failed', 'submission_unknown'].includes(imageTask.status) ||
      textTask && ['failed', 'interrupted', 'submission_unknown'].includes(textTask.status);
    const result = preview && !hasGenerationContent(preview) && (working || failed) ? {
      ...preview, metadata: { ...preview.metadata, status: working ? 'loading' as const : 'error' as const, errorDetails: statusLabel },
    } : preview;
    const material = result ?? node;
    const versions = output?.versions.filter(hasGenerationContent) ?? [];
    const versionIndex = versions.findIndex((version) => version.id === preview?.id);
    const previewVersion = (index: number) => setPreviewedResultIds(current => ({ ...current, [node.id]: index === 0 ? '' : versions[index].id }));
    const retryable = task ? (task.status === 'failed' && !task.material) || canResumeVideo(task) : node.type === CanvasNodeType.Video && Boolean(rawPrompt(node));
    const canPickReference = referencePickerId && (isCanvasReferenceNode(node, displayNodes, canvas.connections) || canvasGeneratorOutputKind(node)) &&
      resolveCanvasLink({ nodeId: node.id, handleType: 'source' }, referencePickerId, displayNodes) &&
      !visibleConnections.some((edge) => edge.fromNodeId === node.id && edge.toNodeId === referencePickerId);
    const referenceState = !referencePickerId ? undefined : referencePickerId === node.id ? 'target' : canPickReference ? 'available' : 'disabled';
    const panelWidth = Math.max(240, Math.min(node.type===CanvasNodeType.Audio?762:660, canvasSize.width - 32));
    const nodeCenter = canvas.viewport.x + (node.position.x + node.width / 2) * canvas.viewport.k;
    const panelCenter = Math.max(panelWidth / 2 + 16, Math.min(canvasSize.width - panelWidth / 2 - 16, nodeCenter));
    const videoInfo = mediaDurations[material.id];
    return <CanvasNode
      key={node.id} data={node} scale={canvas.viewport.k}
      result={result} canLinkOutput={node.type === CanvasNodeType.Config ? Boolean(canvasGeneratorOutputKind(node)) : undefined}
      resultVersions={versions.length > 1 && versionIndex >= 0 ? <div className="fmv-inline-result-versions" aria-label="生成结果版本" data-canvas-no-zoom>
        <button type="button" aria-label="预览较旧结果" disabled={versionIndex >= versions.length - 1} onClick={() => previewVersion(versionIndex + 1)}><ChevronLeft size={13} /></button>
        <span title={versionIndex ? '历史结果预览' : '当前结果'}>{versions.length - versionIndex}/{versions.length}</span>
        <button type="button" aria-label="预览较新结果" disabled={versionIndex === 0} onClick={() => previewVersion(versionIndex - 1)}><ChevronRight size={13} /></button>
      </div> : undefined}
      isSelected={selectedIds.has(node.id)}
      renderMenuItems={() => <>
        <button onClick={() => { copySelection(); setMessage('已复制节点，可在画布中粘贴。'); }}>复制</button>
        <button onClick={() => duplicateSelection()}>创建副本</button>
        {(material.metadata?.storageKey || material.type === 'text' && material.metadata?.content) && <button onClick={() => togglePanel('saveAsset')}>创建资产</button>}
        <button onClick={() => togglePanel('versions')}>版本记录</button>
      </>}
      isRelated={hoveredId !== null && visibleConnections.some((edge) => edge.fromNodeId === hoveredId && edge.toNodeId === node.id || edge.toNodeId === hoveredId && edge.fromNodeId === node.id)}
      isFocusRelated={selectedId === node.id} isConnectionTarget={Boolean(connection && connectionCursor.targetId === node.id)}
      isConnecting={connection !== null} referenceSelectionState={referenceState}
      showPanel={selectedId === node.id && selectedIds.size === 1 && !referencePickerId && !resizing && !dragging}
      panelWidth={panelWidth} panelOffsetX={(panelCenter - nodeCenter) / canvas.viewport.k} panelMaxHeight={Math.max(180, canvasSize.height * .42)}
      statusLabel={statusLabel} showImageInfo
      videoInfo={videoInfo?.src === material.metadata?.content ? videoInfo : undefined}
      batchExpanded={expandedBatches.has(node.id)} groupChildCount={canvas.nodes.filter((item) => item.metadata?.groupId === node.id).length}
      mentionReferences={buildNodeMentionReferences(node, displayNodes, canvas.connections)}
      renderPanel={renderNodePanel} renderNodeContent={renderGeneratorCard} onTextShortcut={applyTextShortcut}
      resultTools={material.metadata?.storageKey && (material.type === 'image' || material.type === 'video') ? <CanvasResultTools key={material.id} projectId={projectId} node={material} onSave={(file) => saveProductionFile(material,file, node)} onSplit={() => setSplitNode(material)} onDraft={(name,prompt) => draftMediaEdit(material,name,prompt,node)}/> : undefined}
      onSelect={activateCanvasNode} onDelete={(id) => removeNodes([id])} deleteDisabled={busy || loadingCanvas} onHoverStart={setHoveredId} onHoverEnd={(id) => setHoveredId((current) => current === id ? null : current)}
      onConnectStart={(event, nodeId, handleType) => {
        if (event.button !== 0 || busyRef.current || loadingCanvas) return;
        event.preventDefault(); event.stopPropagation();
        closeConnectionCreate();
        if (event.type === 'click') {
          setConnection(null);
          const rect = event.currentTarget.getBoundingClientRect();
          openLinkedMenu({ nodeId, handleType }, rect.right + 8, rect.top + rect.height / 2);
          return;
        }
        setConnection({ nodeId, handleType });
        setConnectionCursor({ position: screenToCanvas(event.clientX, event.clientY) });
      }}
      onResizeStart={() => setResizing(true)} onResize={(id, width, height, position) => changeNode(id, { width, height, ...(position ? { position } : {}) })} onResizeEnd={() => setResizing(false)}
      onContentChange={(id, content) => { if (!node.metadata?.publicMaterialRole) changeMetadata(id, { content }); }} onTitleChange={(id, title) => changeNode(id, { title })}
      onToggleBatch={(id) => setExpandedBatches((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; })}
      onSetBatchPrimary={setBatchPrimary} onDuplicateBatchImage={duplicateImage} onDownloadBatchImage={downloadImage}
      onDeleteBatchImage={(id, imageId) => { if (!window.confirm('移除此画布候选？原素材和生成任务继续保存。')) return; const original = canvasRef.current.nodes.find((item) => item.id === id); const images = original?.metadata?.images?.filter((item) => item.id !== imageId) || []; changeMetadata(id, { images, primaryImageId: images[0]?.id, content: images[0]?.content, storageKey: images[0]?.storageKey }); }}
      onRetry={retryable ? retryNode : undefined}
      onViewImage={(item, imageId) => { const image = item.metadata?.images?.find((candidate) => candidate.id === imageId); setPreview(image ? { ...item, metadata: { ...item.metadata, content: image.content, storageKey: image.storageKey } } : item); }}
      onContextMenu={(event, id) => { event.preventDefault(); selectNode(id); }}
    />;
  }

  useEffect(() => {
    if (!zoomMenu && panel !== 'pointer' && panel !== 'help') return;
    const dismiss = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (!target.closest('.fmv-libtv-dock')) setZoomMenu(false);
      if (!target.closest('.fmv-canvas-flyout,.fmv-libtv-rail,.fmv-libtv-dock')) {
        setPanel(current => current === 'pointer' || current === 'help' ? null : current);
      }
    };
    document.addEventListener('pointerdown', dismiss, true);
    return () => document.removeEventListener('pointerdown', dismiss, true);
  }, [zoomMenu, panel]);

  if (!user) return <main className="fmv-loading">{userError || '正在读取账号…'}</main>;
  if (!projectId) return <WorkspaceShell user={user} title="剧情画布"><p className="fmv-error">缺少项目，请从项目列表打开。</p></WorkspaceShell>;
  function togglePanel(next: NonNullable<typeof panel>) {
    setRailMenu(null); setZoomMenu(false); setCreatePosition(null); closeConnectionCreate();
    setPanel(current => current === next ? null : next);
  }
  function selectArea(from: Position, to: Position, additive: boolean) {
    const a = screenToCanvas(from.x, from.y), b = screenToCanvas(to.x, to.y);
    const found = visibleNodes.filter(n => n.position.x < Math.max(a.x, b.x) && n.position.x + n.width > Math.min(a.x, b.x) && n.position.y < Math.max(a.y, b.y) && n.position.y + n.height > Math.min(a.y, b.y));
    const ids = new Set([...(additive ? selectedIds : []), ...found.map(n => n.id)]);
    setSelectedIds(ids); setSelectedId(ids.values().next().value ?? null); setSelectedConnectionId(null);
  }
  async function openDirectorChat(withNodes: boolean, draft?: string) {
    const ids = withNodes ? visibleNodes.filter(node => selectedIds.has(node.id) && !['agent','group'].includes(node.type)).map(node => inlinePreview(node.id)?.id ?? node.id) : [];
    if (ids.length > 20) throw new Error('一次最多引用 20 个节点。');
    if (dirtyRef.current) await saveCanvas();
    const store = useMaterialWorkspace.getState();
    await store.open({ projectId, sceneId });
    const active = useMaterialWorkspace.getState();
    if (active.scope?.projectId !== projectId || active.scope?.sceneId !== sceneId || !active.conversation) throw new Error(active.error || '素材对话未连接。');
    if (ids.length) {
      const refs = await getCanvasChatReferences(projectId, sceneId, ids);
      if (refs.length !== ids.length) throw new Error('部分节点已发生变化，请重新选择。');
      if (useMaterialWorkspace.getState().conversation?.id !== active.conversation.id) return;
      refs.forEach(active.select);
    }
    if (draft) useMaterialWorkspace.setState({ draft });
    setPanel(null);
  }
  async function saveAsset(node: CanvasNodeData, name: string, folder: string) {
    if (dirtyRef.current) await saveCanvas();
    let id = node.metadata?.storageKey;
    const text = !id && node.type === 'text' && node.metadata?.content;
    if (text) id = (await uploadCloudAsset(projectId, new File([text], name + '.txt', { type: 'text/plain' }))).id;
    const current = await getCloudProject(projectId);
    if (!id || !current.project.assets.some(asset => asset.id === id)) throw new Error('请先选择已生成或上传的素材。');
    const updated = { ...current.project, assets: current.project.assets.map(asset => asset.id === id ? { ...asset, name, metadata: { ...asset.metadata, canvasSaved: true, canvasFolder: folder } } : asset) };
    acceptEnvelope(await saveCloudProject(updated));
    if (text) { changeMetadata(node.id, { storageKey: id }); await saveCanvas(); }
    setPanel(null); setMessage('已保存到项目资产库。');
  }
  function applyCharacter(form: CharacterForm) {
    const current = canvasRef.current;
    const previous = selected?.metadata?.characterDesign ? selected : undefined;
    const origin = previous?.position ?? screenToCanvas((containerRef.current?.getBoundingClientRect().left ?? 0) + canvasSize.width / 2, (containerRef.current?.getBoundingClientRect().top ?? 0) + 110);
    const target = previous ?? nodeForMenu('image', origin);
    const prompt = [form.name,form.face,form.body,form.outfit,form.notes].filter(Boolean).join('\n');
    const updated = { ...target, title: form.name, metadata: { ...target.metadata, dramaTask: 'characterImage', characterDesign: form, composerContent: prompt } };
    let nodes = previous ? current.nodes.map(n => n.id === target.id ? updated : n) : [...current.nodes, updated];
    const previousPhoto = previous?.metadata?.characterDesign?.reference;
    let connections = current.connections.filter(edge => !(previousPhoto && previousPhoto !== form.reference && edge.toNodeId === target.id && current.nodes.find(n => n.id === edge.fromNodeId)?.metadata?.storageKey === previousPhoto));
    if (form.reference) {
      const asset = envelopeRef.current?.project.assets.find(a => a.id === form.reference);
      if (!asset || asset.type !== 'image' || !form.authorized) { setError('请重新选择项目参考图片并确认形象授权。'); return; }
      let reference = nodes.find(n => n.type === 'image' && n.metadata?.storageKey === asset.id);
      if (!reference) { reference = applyNodeAsset(makeNode('image', { x: origin.x - 416, y: origin.y }), asset); nodes = [...nodes, reference]; }
      if (!connections.some(e => e.fromNodeId === reference.id && e.toNodeId === target.id)) connections = [...connections, { id: operationId(), fromNodeId: reference.id, toNodeId: target.id }];
    }
    changeCanvas({ ...current, nodes, connections }); setPanel(null); focusNode(target.id);
  }
  function canvasKeys(event: React.KeyboardEvent) {
    if (loadingCanvas || busyRef.current || canvasView !== 'workflow' || event.nativeEvent.isComposing || event.defaultPrevented) return;
    if (!(event.target instanceof Element) || event.target.closest('[role="dialog"],.ant-modal,.material-chat')) return;
    const editing = event.target.closest('input,textarea,select,[contenteditable="true"]');
    if (event.key === 'Escape') {
      if (referencePickerId) {
        deselectCanvas();
      } else if (panel || zoomMenu || railMenu || connectionCreate || createPosition) {
        setPanel(null);
        setZoomMenu(false);
        setRailMenu(null);
        setCreatePosition(null);
        closeConnectionCreate();
      } else if (!editing && !connection) {
        deselectCanvas();
      }
      return;
    }
    if (editing) return;
    const command = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    if (command && key === 'c' && selectedIds.size) copySelection();
    else if (command && key === 'v' && clipboard.current) duplicateSelection(true);
    else if (command && key === 'd' && selectedIds.size) duplicateSelection();
    else if (command || event.altKey) return;
    else if (key === '0') zoomAtCenter(1);
    else if (key === '1') fitAllNodes();
    else if (key === '=' || key === '+') zoomAtCenter(Math.min(5, canvas.viewport.k * 1.25));
    else if (key === '-') zoomAtCenter(Math.max(.05, canvas.viewport.k / 1.25));
    else if (key === '?') togglePanel('keys');
    else return;
    event.preventDefault();
  }
  const linkPreview = connection || connectionCreate?.connection;
  const content = <>
    <div className="fmv-studio-toolbar">{embedded ? <label className="flex items-center gap-2 text-sm">所属场景<select data-testid="material-scene-select" className="rounded border border-white/20 bg-[#24211d] p-2" value={sceneId} disabled={busy || loadingCanvas} onChange={(event) => { void selectScene(event.target.value); }}>{scenes.map((scene) => <option key={scene.id} value={scene.id}>{sceneTitle(scene.id)}</option>)}</select></label> : <nav className="fmv-studio-links" aria-label="项目创作入口"><Link href={`/zh-CN/editor?id=${encodeURIComponent(projectId)}`}>故事总画布</Link><span>剧情素材画布</span><Link href={`/zh-CN/nodes?id=${encodeURIComponent(projectId)}`}>场景时间线</Link><Link href="/zh-CN/assets">素材库</Link></nav>}<div className="flex items-center gap-3"><span role="status" data-testid="save-status" className="fmv-save-state">{loadingCanvas ? '正在读取画布' : dirty ? inputDirty ? '有未保存的内容或连线' : '视口有未保存的调整' : `已保存 · 版本 ${envelope?.revision ?? '…'}`}</span><button data-testid="canvas-save" className="fmv-save-button flex items-center gap-2" disabled={busy || loadingCanvas || !sceneId} onClick={() => { void perform(async () => { await saveCanvas(); }); }}><Save size={15} />保存画布</button></div></div>
    {error && !reviewTask && <p role="alert" className="fmv-error fmv-studio-error">{error}</p>}{message && <p role="status" className="fmv-stage-note">{message}</p>}
    <div className="fmv-studio-body">
      {!embedded && <aside className="fmv-scenes"><h2>剧情场景</h2>{scenes.map((scene) => <button key={scene.id} data-active={scene.id === sceneId} data-testid="scene-item" disabled={busy} onClick={() => { void selectScene(scene.id); }}><Film size={14} />{sceneTitle(scene.id)}</button>)}{scenes.length === 0 && <p>创建剧情场景后开始组织素材。</p>}<form onSubmit={(event) => { void createScene(event); }}><label>新场景名称<input data-testid="scene-title" value={newSceneTitle} onChange={(event) => setNewSceneTitle(event.target.value)} required maxLength={120} /></label><button data-testid="scene-create" disabled={busy || !envelope}>创建场景</button></form></aside>}
      <section className="fmv-canvas-column" aria-label="剧情素材画布"><CanvasProviders>
        <div ref={shellRef} className="fmv-libtv-shell" tabIndex={-1} onKeyDown={canvasKeys} data-chat-layout={chatPlacement} inert={loadingCanvas} onLoadedMetadataCapture={(event) => {
          const media = event.target;
          if (!(media instanceof HTMLVideoElement) || !media.dataset.canvasVideo || !Number.isFinite(media.duration)) return;
          const id = media.dataset.canvasVideo, src = media.getAttribute('src') || '', seconds = media.duration;
          const width = media.videoWidth, height = media.videoHeight;
          setMediaDurations((current) => {
            const previous = current[id];
            if (previous?.src === src && previous.seconds === seconds && previous.width === width && previous.height === height) return current;
            return { ...current, [id]: { src, seconds, width, height } };
          });
        }} style={{ height: '100%', '--canvas-panel': theme.node.panel, '--canvas-fill': theme.node.fill, '--canvas-border': theme.node.stroke, '--canvas-text': theme.node.text, '--canvas-toolbar': theme.toolbar.panel, '--canvas-hover': theme.toolbar.itemHover } as React.CSSProperties}>
          <div className="fmv-libtv-viewport" data-canvas-view={canvasView}>
          <div className="fmv-libtv-view-switch" role="group" aria-label="画布视图" data-canvas-no-zoom>
            <button type="button" aria-pressed={canvasView === 'workflow'} onClick={() => setCanvasView('workflow')}>工作流</button>
            <button type="button" aria-pressed={canvasView === 'storyboard'} onClick={() => { setCanvasView('storyboard'); setRailMenu(null); closeConnectionCreate(); setCreatePosition(null); }}>故事板</button>
          </div>
          <div className="fmv-libtv-workflow" hidden={canvasView !== 'workflow'}>
          <div className="fmv-libtv-rail" role="toolbar" aria-label="画布工具" data-canvas-no-zoom>
            <button data-testid="canvas-add-node" aria-label="添加节点" title="添加节点" aria-expanded={railMenu !== null} disabled={!sceneId || loadingCanvas} onClick={(event) => {
              const viewport = containerRef.current?.getBoundingClientRect();
              const trigger = event.currentTarget.getBoundingClientRect();
              if (viewport) setRailMenu((current) => current ? null : { x: trigger.right - viewport.left + 12, y: Math.max(8, Math.min(viewport.height - 380, trigger.top - viewport.top)) });
            }}><Plus size={20} /></button>
            <button aria-label="移动工具" title="移动工具" aria-expanded={panel === 'pointer'} onClick={() => togglePanel('pointer')}><Hand size={18}/></button>
            <button aria-label="节点列表" title="节点列表" aria-pressed={assetPane === 'nodes'} onClick={() => setAssetPane(current => current === 'nodes' ? null : 'nodes')}><Layers size={18}/></button>
            <button aria-label="素材库" title="资产库" aria-pressed={assetPane === 'assets'} onClick={() => setAssetPane(current => current === 'assets' ? null : 'assets')}><FolderOpen size={18} /></button>
            <button aria-label="生成历史" title="生成历史" onClick={() => openAssetLibrary('history')}><History size={18} /></button>
            <button aria-label="创作广场" title="创作广场" onClick={() => togglePanel('character')}><Shapes size={18}/></button>
            <button aria-label="全部功能" title="全部功能" onClick={() => togglePanel('tools')}><Boxes size={18}/></button>
            <button aria-label="创作对话" title="创作对话" onClick={() => { void perform(() => openDirectorChat(false)); }}><MessageSquare size={18}/></button>
          </div>
          <div className="fmv-libtv-dock" role="toolbar" aria-label="视口" data-canvas-no-zoom>
            <button aria-label="资产管理" title="资产管理" aria-pressed={assetPane !== null} onClick={() => setAssetPane((pane) => pane ? null : 'nodes')}><PanelRight size={16} /></button>
            <button aria-label="显示全部节点" title="显示全部节点" data-testid="canvas-fit-all" onClick={fitAllNodes}><Focus size={16} /></button>
            <button aria-label="切换小地图" title="切换小地图" aria-pressed={miniMapOpen} onClick={() => setMiniMapOpen((value) => !value)}><MapIcon size={16} /></button>
            <button aria-label={showLinks ? '隐藏节点连线' : '显示节点连线'} title={showLinks ? '隐藏节点连线' : '显示节点连线'} aria-pressed={showLinks} onClick={() => setShowLinks((value) => !value)}><Network size={16} /></button>
            <button aria-label="缩放选项" title="缩放选项" aria-expanded={zoomMenu} onClick={() => setZoomMenu((value) => !value)}>{Math.round(canvas.viewport.k * 100)}%</button>
            <button aria-label="快捷键" title="快捷键" onClick={() => togglePanel('keys')}><Keyboard size={16}/></button>
            <button aria-label="教程" title="教程" onClick={() => togglePanel('help')}><CircleHelp size={16}/></button>
            {zoomMenu && <div className="fmv-libtv-zoom-menu" aria-label="缩放选项">
              <form onSubmit={event => { event.preventDefault(); zoomAtCenter(Number(new FormData(event.currentTarget).get('zoom')) / 100); setZoomMenu(false); }}><input name="zoom" aria-label="缩放百分比" type="number" min="5" max="500" required defaultValue={Math.round(canvas.viewport.k * 100)}/><span>%</span></form>
              <button type="button" onClick={() => zoomAtCenter(Math.min(5, canvas.viewport.k * 1.25))}>放大</button>
              <button type="button" onClick={() => zoomAtCenter(Math.max(.05, canvas.viewport.k / 1.25))}>缩小</button>
              <button type="button" onClick={() => { fitAllNodes(); setZoomMenu(false); }}>显示全部节点</button>
              {[0.5, 1, 2].map(k => <button type="button" key={k} onClick={() => { zoomAtCenter(k); setZoomMenu(false); }}>{k * 100}%</button>)}
              <label><input type="checkbox" checked={miniMapOpen} onChange={event => setMiniMapOpen(event.target.checked)}/>小地图</label>
              <label><input type="checkbox" checked={snap} onChange={event => setSnap(event.target.checked)}/>网格吸附</label>
            </div>}
          </div>
          <input ref={fileInputRef} data-testid="canvas-import-file" type="file" accept="image/*,video/*,audio/*,.txt,.md" hidden onChange={(event) => { void upload(event.target.files); }} />
          {railMenu && <NodeCreateMenu position={railMenu} screenAnchor={railMenu} onCreate={(type, settings) => addNode(type, undefined, settings?.legacyScript)} onUpload={() => openAssetUpload()} onHistory={() => openAssetLibrary('history')} onClose={() => setRailMenu(null)} />}
          <InfiniteCanvas containerRef={containerRef} viewport={canvas.viewport} onSelectArea={referencePickerId ? undefined : selectArea}
            onViewportChange={(viewport) => { if (!loadingCanvas && sceneId) changeCanvas((value) => ({ ...value, viewport }), true); }}
            onCanvasDeselect={deselectCanvas} onNodeSelect={activateCanvasNode} onNodeDragStart={beginCanvasNodeDrag} onDragChange={setDragging} onGestureStart={cancelCanvasFocus}
            onCanvasDoubleClick={(event) => { if (!referencePickerId) setCreatePosition(screenToCanvas(event.clientX, event.clientY)); }}>
            <svg className="pointer-events-none absolute left-0 top-0 h-[10000px] w-[10000px] overflow-visible" style={{ zIndex: 0 }}><g>{showLinks && visibleConnections.map((edge) => { const from = visibleNodes.find((node) => node.id === edge.fromNodeId), to = visibleNodes.find((node) => node.id === edge.toNodeId); return from && to ? <ConnectionPath key={edge.id} connection={edge} from={from} to={to} active={selectedConnectionId === edge.id} onSelect={() => { setSelectedConnectionId(edge.id); setSelectedId(null); setSelectedIds(new Set()); }} /> : null; })}{linkPreview && <ActiveConnectionPath node={visibleNodes.find((node) => node.id === linkPreview.nodeId)} handle={linkPreview} mouseWorld={connectionCreate?.position ?? connectionCursor.position} target={connection ? visibleNodes.find((node) => node.id === connectionCursor.targetId) : undefined} />}</g></svg>
            {visibleNodes.map(renderCanvasNode)}
            {createPosition && <NodeCreateMenu position={createPosition} scale={canvas.viewport.k} onCreate={(type, settings) => addNode(type, createPosition, settings?.legacyScript)} onUpload={() => openAssetUpload(createPosition)} onHistory={() => openAssetLibrary('history', createPosition)} onClose={() => setCreatePosition(null)} />}
            {connectionCreate && <ConnectionCreateMenu pending={connectionCreate} scale={canvas.viewport.k} onCreate={addLinkedGeneration} onClose={closeConnectionCreate} />}
          </InfiniteCanvas>
          {miniMapOpen && <Minimap nodes={visibleNodes} viewport={canvas.viewport} viewportSize={canvasSize} onViewportChange={(viewport) => changeCanvas((value) => ({ ...value, viewport }), true)} />}
          {canvas.nodes.length === 0 && <div className="fmv-libtv-start" data-canvas-no-zoom><p>双击画布 自由生成节点</p><div>
            <button onClick={() => addNode('image')}><ImageIcon size={16} />图片生成</button>
            <button onClick={() => addNode('video')}><Film size={16} />视频生成</button>
            <button onClick={() => addNode('audio')}><AudioLines size={16} />音频</button>
            <button onClick={() => addNode('script')}><NotebookText size={16} />剧本生成</button>
            <button onClick={() => addNode('smart-edit')}><Scissors size={16} />智能剪辑</button>
          </div></div>}
          {referencePickerId && <button className="fmv-node-reference-selection" onClick={() => setReferencePickerId(null)}>点击画布节点添加参考 · 完成选择</button>}
          {selectedConnectionId && <div className="fmv-node-actions"><button data-testid="canvas-disconnect-link" onClick={() => {
            const edge = visibleConnections.find((item) => item.id === selectedConnectionId);
            if (edge) disconnectReference(edge.fromNodeId, edge.toNodeId, new Set(connectionIdsByVisibleId.get(edge.id)));
            setSelectedConnectionId(null);
          }}><Trash2 size={14} />断开连接</button></div>}
          {selected && !referencePickerId && <div className="fmv-node-actions" data-testid="canvas-node-actions" data-canvas-no-zoom>
            <button onClick={() => focusNode(selected.id)}>聚焦节点</button>
            <button data-testid="canvas-copy-selection" onClick={() => duplicateSelection()}><Copy size={14} />创建副本</button>
            <button onClick={() => { void perform(() => openDirectorChat(true)); }}>发送到创作对话</button>
            {selectedIds.size === 1 && <button onClick={() => togglePanel('versions')}>版本记录</button>}
            {(selectedResult?.metadata?.storageKey || selectedResult?.type === 'text' && selectedResult.metadata?.content) && <button onClick={() => togglePanel('saveAsset')}>创建资产</button>}
            {selected.metadata?.characterDesign && <button onClick={() => togglePanel('character')}><UserRound size={14}/>编辑角色</button>}
            <button onClick={() => { mediaReferenceTarget.current = null; uploadTarget.current = selected.id; fileInputRef.current?.click(); }}><Upload size={14} />导入素材</button>
            {canGroupSelectedNodes(selectedIds, canvas.nodes) && <button data-testid="canvas-group-selection" onClick={groupSelection}><Group size={14} />组合</button>}
            {canUngroupSelectedNodes(selectedIds, canvas.nodes) && <button data-testid="canvas-ungroup-selection" onClick={ungroupSelection}><Ungroup size={14} />取消组合</button>}
            {selectedResult?.metadata?.storageKey && <a href={assetContentUrl(selectedResult.metadata.storageKey)} download={selected.title}>下载素材</a>}
            {selectedResult?.metadata?.storageKey && ['image', 'video', 'audio'].includes(selectedResult.type) && <button data-testid="canvas-adopt-to-timeline" disabled={busy || loadingCanvas} onClick={() => { void perform(async () => {
              if (dirtyRef.current) await saveCanvas();
              const current = await getCloudProject(projectId);
              const saved = await addAssetToSceneTimeline(projectId, sceneId, selectedResult.metadata!.storageKey!, current.revision);
              acceptEnvelope(saved); setMessage('素材已加入当前场景时间线，可在故事总画布中剪辑和设置互动。');
            }); }}>加入场景时间线</button>}
            {selectedResult?.type === CanvasNodeType.Image && selectedResult.metadata?.storageKey && <button data-testid="canvas-image-split" disabled={busy} onClick={() => setSplitNode(selectedResult)}><Grid2x2 size={14} />切片</button>}
            <button data-testid="canvas-delete-selection" disabled={busy || loadingCanvas} onClick={() => removeNodes([...selectedIds])}><Trash2 size={14} />删除</button>
          </div>}
          </div>
          {canvasView === 'storyboard' && <section className="fmv-libtv-storyboard" aria-label="故事板">
            <header><h2>故事板</h2><div><button onClick={() => { void perform(() => openDirectorChat(false)); }}>创作对话</button><select aria-label="对话布局" value={chatPlacement} onChange={e => setChatPlacement(e.target.value as typeof chatPlacement)}><option value="left">对话居左</option><option value="right">对话居右</option><option value="floating">浮窗对话</option></select><button aria-pressed={showStoryAssets} onClick={() => setShowStoryAssets(!showStoryAssets)}>{showStoryAssets ? '收起素材' : '显示素材'}</button></div></header>
            {showStoryAssets && <nav aria-label="故事板分类">{['全部','角色','场景','物品','分镜','片段','成片'].map(kind => <button aria-pressed={storyFilter === kind} key={kind} onClick={() => setStoryFilter(kind)}>{kind}</button>)}</nav>}
            {showStoryAssets && scenes.map((scene) => {
              const latestCanvas = scene.id === sceneId ? canvas : envelope?.canvases.find((item) => item.sceneId === scene.id);
              const cards = latestCanvas?.nodes.filter(node => {
                if (!node.metadata?.storageKey || !['image','video'].includes(node.type)) return false;
                const producer = latestCanvas.connections.filter(e => e.toNodeId === node.id).map(e => latestCanvas.nodes.find(n => n.id === e.fromNodeId)).find(n => n?.type === 'config');
                const task = producer?.metadata?.dramaTask || node.metadata.dramaTask;
                const category = node.type === 'video' ? task === 'join' ? '成片' : '片段' : task === 'characterImage' ? '角色' : task === 'sceneImage' ? '场景' : task === 'propImage' ? '物品' : '分镜';
                return storyFilter === '全部' || category === storyFilter;
              }) ?? [];
              return <article key={scene.id}>
                <button type="button" onClick={() => { void selectScene(scene.id); setCanvasView('workflow'); }}>{sceneTitle(scene.id)}</button>
                <div>{cards.filter((node) => node.metadata?.storageKey).map((node) => <figure key={node.id}>
                  {node.type === CanvasNodeType.Image ? <img src={assetContentUrl(node.metadata!.storageKey!)} loading="lazy" alt="" /> : <video src={assetContentUrl(node.metadata!.storageKey!)} controls preload="none" />}
                  <figcaption>{node.title}</figcaption>
                </figure>)}</div>{!cards.length && <p>当前分类暂无素材</p>}
              </article>;
            })}
          </section>}
          </div>
          {assetPane && <CanvasBrowser section={assetPane} nodes={displayNodes} assets={envelope?.project.assets ?? []} generated={generatedAssetIds} selectedId={selectedId}
            onSection={setAssetPane} onClose={() => setAssetPane(null)} onFocus={id => { setCanvasView('workflow'); focusNode(id); }} onAsset={asset => { setCanvasView('workflow'); addProjectAsset(asset); }} />}
          {!embedded && chatOpen && <aside className="fmv-canvas-chat"><MaterialChat onClose={() => useMaterialWorkspace.setState({chatOpen:false})}/></aside>}
          {panel === 'pointer' && <div className="fmv-canvas-flyout" role="group" aria-label="画布移动手势">
            <p>直接拖动，平移画布。</p>
            <p>长按卡片再拖动，移动卡片。</p>
            <p>按住 Shift 拖动画布，框选卡片。</p>
          </div>}
          {panel === 'help' && <div className="fmv-canvas-flyout fmv-help-links" aria-label="教程与帮助"><a href="/zh-CN/studio/guide" target="_blank" rel="noreferrer">使用教程 ↗</a><button onClick={() => togglePanel('keys')}>快捷键</button><a href="https://github.com/Ethan3114/SceneTurn/issues" target="_blank" rel="noreferrer">问题反馈 ↗</a></div>}
          <Modal title="快捷键" open={panel === 'keys'} footer={null} onCancel={() => setPanel(null)}><CanvasShortcutTable/></Modal>
          <Modal title="全部功能" open={panel === 'tools'} destroyOnHidden footer={null} onCancel={() => setPanel(null)}><CanvasToolDirectory assets={envelope?.project.assets ?? []} onChoose={kind => { if (kind === 'character') setPanel('character'); else { setCanvasView('workflow'); addNode(kind); setPanel(null); } }} onResultTool={(asset, action) => {
            const node = displayNodes.find(n => n.type === asset.type && n.metadata?.storageKey === asset.id) ?? addProjectAsset(asset);
            if (!node) return;
            setCanvasView('workflow');
            focusNode(node.id);
            if (action === '图片切片') setSplitNode(node);
            else if (action === '编辑音频') editAudio(node);
            else setDirectoryEdit({ node, action });
            setPanel(null);
          }}/></Modal>
          {directoryEdit && <EditResult projectId={projectId} node={directoryEdit.node} action={directoryEdit.action} close={() => setDirectoryEdit(null)} onSave={file => saveProductionFile(directoryEdit.node,file)} onDraft={(name,prompt) => draftMediaEdit(directoryEdit.node,name,prompt)}/>}
          <Modal title="角色造型室" open={panel === 'character'} destroyOnHidden footer={null} onCancel={() => setPanel(null)}>
            <CharacterStudio initial={selected?.metadata?.characterDesign || emptyCharacterForm} assets={envelope?.project.assets ?? []} onApply={applyCharacter} onImport={async file => { const asset = await uploadCloudAsset(projectId, file); acceptEnvelope(await getCloudProject(projectId)); return asset; }}/>
          </Modal>
          {selected && selectedResult && <Modal title="创建资产" open={panel === 'saveAsset'} destroyOnHidden footer={null} onCancel={() => setPanel(null)}><form className="fmv-canvas-form" onSubmit={e => { e.preventDefault(); const fields = new FormData(e.currentTarget); const name = String(fields.get('name') || '').trim(); if (name) void perform(() => saveAsset(selectedResult, name, String(fields.get('folder') || '').trim())); }}>
            <label>资产名称<input name="name" required defaultValue={selected.title} maxLength={160}/></label><label>文件夹<input name="folder" placeholder="项目资产" maxLength={100}/></label><button disabled={busy}>保存到资产库</button></form></Modal>}
          {panel === 'versions' && selected && <Modal title="版本记录" open footer={null} onCancel={() => setPanel(null)}><CanvasVersionHistory key={selected.id} projectId={projectId} sceneId={sceneId} nodeId={selected.id}/></Modal>}
          <Modal title={preview?.title || '素材预览'} open={preview !== null} footer={null} onCancel={() => setPreview(null)} width={960}>{preview && <img src={preview.metadata?.storageKey ? assetContentUrl(preview.metadata.storageKey) : preview.metadata?.content} alt={preview.title} style={{ width: '100%', maxHeight: '75dvh', objectFit: 'contain' }} />}</Modal>
          {productionNode && (productionNode.metadata?.productionTool==='director' ? <DirectorEditor key={productionNode.id} projectId={projectId} value={productionNode.metadata.directorScene} images={displayNodes.filter(n=>n.type===CanvasNodeType.Image)} onChange={directorScene=>changeMetadata(productionNode.id,{directorScene})} onSave={file=>saveProductionFile(productionNode,file)} onClose={()=>setProductionEditorId(null)}/> : <MediaCutEditor key={productionNode.id} projectId={projectId} sources={productionSources(productionNode.id)} audioOnly={productionNode.metadata?.productionTool==='audio-edit'} settings={productionNode.metadata?.editSettings || {}} onChange={editSettings=>changeMetadata(productionNode.id,{editSettings})} onSave={file=>saveProductionFile(productionNode,file)} onClose={()=>setProductionEditorId(null)}/>)}
          {scriptTableNode?.metadata?.scriptRows && <CanvasScriptTable title={scriptTableNode.title} legacy={scriptTableNode.metadata.scriptVersion === 'legacy' || scriptTableNode.title === '脚本（旧版）'} rows={scriptTableNode.metadata.scriptRows} onChange={(rows) => updateScriptTable(scriptTableNode.id, rows)} onClose={() => setScriptTableId(null)}
            images={displayNodes.flatMap((node) => node.type === 'image' && node.metadata?.storageKey && node.metadata.content ? [{ id: node.id, title: node.title, url: node.metadata.content }] : [])}
            onPrepare={(ids, kind) => void perform(async () => {
              const result = prepareShotNodes(canvasRef.current, scriptTableNode, ids, kind, nodeForMenu);
              if (result.first) { changeCanvas(result.canvas); setScriptTableId(null); focusNode(result.first); }
            })} />}
          <Modal title={libraryMode === 'history' ? '从生成历史选择' : '素材库'} open={libraryMode !== null} footer={null} onCancel={() => { setLibraryMode(null); mediaReferenceTarget.current = null; pickerPosition.current = undefined; }} width={660}>
            <div className="fmv-browser-tabs"><button aria-pressed={libraryMode === 'assets'} onClick={() => { setLibraryMode('assets'); setLibraryError(''); }}>项目素材</button><button aria-pressed={libraryMode === 'history'} onClick={() => { setLibraryMode('history'); setLibraryError(''); }}>生成历史</button></div>{libraryError && <p role="alert">{libraryError}</p>}
            <div className="fmv-libtv-library">{availableAssets.length ? availableAssets.map((asset) => <button type="button" key={asset.id} onClick={() => addProjectAsset(asset)}>
              {asset.type === 'image' ? <img src={assetContentUrl(asset.id)} alt="" loading="lazy" /> : asset.type === 'video' ? <Film size={20} /> : asset.type === 'audio' ? <AudioLines size={20} /> : <Type size={20} />}
              <span>{asset.name}</span>
            </button>) : <p>{libraryMode === 'history' ? '当前项目暂无已完成的媒体生成' : '当前项目暂无素材'}</p>}</div>
          </Modal>
          <CanvasNodeSplitDialog key={splitNode?.id || 'closed'} dataUrl={splitNode?.metadata?.storageKey ? assetContentUrl(splitNode.metadata.storageKey) : ''} open={splitNode !== null} busy={busy} onClose={() => { if (!busy) setSplitNode(null); }} onConfirm={(params) => { void splitImage(params); }} />
        </div>
      </CanvasProviders></section>
    </div>
    {reviewTask && <VideoGenerationReview
      task={reviewTask}
      revision={envelope?.revision || 0}
      inputDirty={inputDirty}
      busy={busy || loadingCanvas}
      error={error}
      sourceAvailable={canvas.nodes.some((item) => item.id === reviewTask.sourceNodeId)}
      onRequote={regenerate}
      onConfirm={(value) => { void confirm(value); }}
      onClose={() => { setReviewTaskId(null); setError(''); }}
    />}
  </>;
  return embedded ? <div className="fmv-creation-workspace fmv-material-embedded" data-testid="embedded-material-canvas">{content}</div> : <WorkspaceShell user={user} title={envelope?.project.title || '正在加载项目…'} canvas>{content}</WorkspaceShell>;
}
