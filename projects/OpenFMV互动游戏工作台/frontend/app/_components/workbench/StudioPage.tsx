'use client';

import { addAssetToSceneTimeline } from '@/app/_utils/cloudApi';
import '@/app/_features/creation-canvas/embedded-materials.css';

import React, { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { Modal } from 'antd';
import { z } from 'zod';
import { Bot, Copy, FileText, Film, Grid2x2, Group, Hand, MousePointer2, Save, Settings2, Trash2, Ungroup, Upload } from 'lucide-react';

import { CanvasProviders } from '@/app/_features/creation-canvas/CanvasProviders';
import { GenerationTaskCard, taskLabels } from '@/app/_features/creation-canvas/GenerationTaskCard';
import { TextGenerationTaskCard } from '@/app/_features/creation-canvas/TextGenerationTaskCard';
import { nodeDramaTask } from '@/app/_features/creation-canvas/drama-tasks';
import { buildShotFrameGraph } from '@/app/_features/creation-canvas/plot-shots';
import { upstreamBranchEdges } from '@/app/_features/creation-canvas/agent-branches';
import { mergeCanvasSnapshots, sameValue } from '@/app/_features/creation-canvas/merge-canvas';
import { CanvasNodeSplitDialog } from '@/app/_features/creation-canvas/CanvasNodeSplitDialog';
import { InfiniteCanvas } from '@/app/_features/creation-canvas/upstream/infinite-canvas';
import { CanvasNode } from '@/app/_features/creation-canvas/upstream/canvas-node';
import { CanvasConfigNodePanel } from '@/app/_features/creation-canvas/upstream/canvas-config-node-panel';
import { ActiveConnectionPath, ConnectionPath } from '@/app/_features/creation-canvas/upstream/canvas-connections';
import { NodeCreateMenu } from '@/app/_features/creation-canvas/upstream/canvas-create-menus';
import { CanvasZoomControls } from '@/app/_features/creation-canvas/upstream/canvas-zoom-controls';
import { Minimap } from '@/app/_features/creation-canvas/upstream/canvas-mini-map';
import { registerBuiltinNodes } from '@/app/_features/creation-canvas/upstream/builtin-nodes';
import { getNodeSpec } from '@/app/_features/creation-canvas/upstream/canvas-constants';
import { applyGroupSelection, applyUngroupSelection, canGroupSelectedNodes, canUngroupSelectedNodes, collectGroupMemberNodes, focusViewportForNode, getGroupWrapRect, nodeBounds, normalizeConnection } from '@/app/_features/creation-canvas/upstream/canvas-node-geometry';
import { buildNodeGenerationContext, buildNodeGenerationInputs } from '@/app/_features/creation-canvas/upstream/canvas-node-generation';
import { buildNodeMentionReferences, isCanvasReferenceNode } from '@/app/_features/creation-canvas/upstream/canvas-resource-references';
import { canvasThemes } from '@/app/_features/creation-canvas/upstream/canvas-theme';
import { useThemeStore } from '@/app/_features/creation-canvas/upstream/use-theme-store';
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasNodeMetadata, type ConnectionHandle, type Position } from '@/app/_features/creation-canvas/upstream/canvas';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { acceptProjectEnvelope, adoptGeneration, applyTextGeneration, assetContentUrl, confirmGeneration, createCloudScene, createTextGeneration, getCloudCanvas, getCloudProject, getGenerations, getTextGeneration, getTextGenerations, getTextSettings, getWanSettings, joinGeneration, operationId, quoteGeneration, resumeGeneration, saveCloudCanvas, splitCloudImage, stopTextGeneration, uploadCloudAsset, type CanvasSnapshot, type GenerationView, type ImageSplitParams, type ProjectEnvelope, type TextGenerationView, type TextSettings } from '@/app/_utils/cloudApi';
import { WorkspaceShell, useWorkspaceUser, errorText } from './WorkspaceShell';
import AgentCanvasNode from './AgentCanvasNode';

registerBuiltinNodes();
const emptyCanvas = (sceneId: string): CanvasSnapshot => ({ sceneId, nodes: [], connections: [], viewport: { x: 40, y: 100, k: 1 } });
const rawPrompt = (node: CanvasNodeData) => node.metadata?.composerContent ?? node.metadata?.prompt ?? (node.type === CanvasNodeType.Text ? node.metadata?.content : '') ?? '';
function makeNode(type: string, position: Position): CanvasNodeData {
  if (type === 'agent') return { id: operationId(), type, title: '素材创作助手', position, width: 460, height: 720, metadata: { agentWorkspace: 'scene', agentRole: 'writer', agentGoal: '', agentExecutor: 'platform', agentTargetIds: [] } };
  const spec = getNodeSpec(type);
  return { id: operationId(), type, title: type === CanvasNodeType.Config ? '生成配置' : spec.title, position, width: spec.width, height: spec.height, metadata: { ...spec.metadata, ...(type === CanvasNodeType.Config ? { generationMode: 'video', dramaTask: 'clip' } : {}) } };
}
function generationInputProblem(node: CanvasNodeData, nodes: CanvasNodeData[], connections: CanvasConnection[]) {
  const task = nodeDramaTask(node);
  if (task.id === 'join') {
    const resources = buildNodeGenerationInputs(node.id, nodes, connections).flatMap((input) => input.type === 'group' ? input.children : [input]);
    return new Set(resources.filter((input) => input.type === 'video').map((input) => input.nodeId)).size >= 2 ? '' : '请连接至少两个已有视频素材的节点。';
  }
  const prompt = rawPrompt(node);
  const tokens = [...prompt.matchAll(/@\[node:([^\]]+)\]/g)];
  if (node.type === CanvasNodeType.Config && node.metadata?.composerContent?.trim() && tokens.length) {
    const inputIds = new Set(buildNodeGenerationInputs(node.id, nodes, connections).map((input) => input.nodeId));
    if (tokens.some((token) => !inputIds.has(token[1]))) return '组合输入引用了未连接或已移除的节点，请重新选择参考。';
  }
  const context = buildNodeGenerationContext(node.id, nodes, connections, prompt);
  if (Array.from(prompt).length > 20000 || Array.from(context.prompt.trim()).length > 20000) return '本次完整输入超过20000个字符，请缩短要求或参考文字。';
  if (task.kind !== 'image' && (context.referenceImages.length || context.referenceVideos.length || context.referenceAudios.length)) return '当前生成支持文字输入，请移除本次选中的媒体参考。';
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
  if (task.status === 'ready' && task.assetId) return { ...node, metadata: { ...node.metadata, content: assetContentUrl(task.assetId), storageKey: task.assetId, status: 'success', pendingResult: false } };
  if (['failed', 'query_interrupted', 'submission_unknown'].includes(task.status)) {
    const problem = typeof task.error === 'string' ? task.error : task.error?.message;
    return { ...node, metadata: { ...node.metadata, ...media, status: 'error', errorDetails: `${taskLabels[task.status]}${problem ? `：${problem}` : ''}` } };
  }
  return { ...node, metadata: { ...node.metadata, ...media, status: ['queued', 'submitting', 'processing', 'downloading'].includes(task.status) ? 'loading' : 'idle' } };
}

export interface MaterialCanvasHandle { save: () => Promise<void> }

const publicScript = z.object({ plot: z.string(), scenes: z.array(z.object({ heading: z.string(), action: z.string(), dialogues: z.array(z.object({ speaker: z.string(), line: z.string() })) })) });

function materialDisplayNode(node: CanvasNodeData): CanvasNodeData {
  const professional = node.metadata?.creativeArtifact;
  if (!professional || typeof professional !== 'object') return node;
  const role = z.object({ role: z.string() }).parse(professional).role;
  let content = '在岗位区域查看成果，通过“问问 AI”提出修改。';
  if (role === 'writer') {
    const script = publicScript.parse(professional);
    content = [script.plot, ...script.scenes.flatMap((scene) => [scene.heading, scene.action, ...scene.dialogues.map((line) => `${line.speaker}：${line.line}`)])].join('\n\n');
  }
  return { ...node, metadata: { ...node.metadata, content, texts: [], primaryTextId: undefined } };
}

export default function StudioPage({ projectId, initialSceneId, embedded = false, saveRef }: { projectId: string; initialSceneId?: string; embedded?: boolean; saveRef?: React.Ref<MaterialCanvasHandle> }) {
  const { user, error: userError } = useWorkspaceUser();
  const [envelope, setEnvelope] = useState<ProjectEnvelope | null>(null);
  const envelopeRef = useRef<ProjectEnvelope | null>(null);
  const [sceneId, setSceneId] = useState(initialSceneId || '');
  const [canvas, setCanvas] = useState<CanvasSnapshot>(emptyCanvas(initialSceneId || ''));
  const canvasRef = useRef(canvas);
  const savedCanvasRef = useRef(canvas);
  const editVersion = useRef(0);
  const contentVersion = useRef(0);
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const [inputDirty, setInputDirty] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [tasks, setTasks] = useState<GenerationView[]>([]);
  const [textTasks, setTextTasks] = useState<TextGenerationView[]>([]);
  const [textSettings, setTextSettings] = useState<TextSettings | null>(null);
  const syncedTextRevisions = useRef(new Set<string>());
  const [model, setModel] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [loadingCanvas, setLoadingCanvas] = useState(true);
  const [tool, setTool] = useState<'select' | 'pan'>('select');
  const [miniMapOpen, setMiniMapOpen] = useState(false);
  const [newSceneTitle, setNewSceneTitle] = useState('');
  const [referencePickerId, setReferencePickerId] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionHandle | null>(null);
  const [mouseWorld, setMouseWorld] = useState<Position>({ x: 0, y: 0 });
  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null);
  const [createPosition, setCreatePosition] = useState<Position | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [resizing, setResizing] = useState(false);
  const [expandedBatches, setExpandedBatches] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<CanvasNodeData | null>(null);
  const [splitNode, setSplitNode] = useState<CanvasNodeData | null>(null);
  const [mediaDurations, setMediaDurations] = useState<Record<string, { src: string; seconds: number }>>({});
  const containerRef = useRef<HTMLDivElement>(null);
  const [canvasSize, setCanvasSize] = useState({ width: 600, height: 640 });
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadTarget = useRef<string | null>(null);
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
  const selectNode = useCallback((id: string) => { setSelectedId(id); setSelectedIds(new Set([id])); setSelectedConnectionId(null); setCreatePosition(null); }, []);
  const removeNodes = useCallback((ids: string[]) => {
    if (busyRef.current || loadingCanvas) return;
    const deletedIds = new Set(ids.filter((id) => canvasRef.current.nodes.some((node) => node.id === id)));
    if (!deletedIds.size || !window.confirm(`删除选中的 ${deletedIds.size} 个画布节点及其连线？生成任务、素材和历史记录继续保存。`)) return;
    changeCanvas((value) => ({ ...value,
      nodes: value.nodes.filter((node) => !deletedIds.has(node.id)).map((node) => node.metadata?.groupId && deletedIds.has(node.metadata.groupId) ? { ...node, metadata: { ...node.metadata, groupId: undefined } } : node),
      connections: value.connections.filter((edge) => !deletedIds.has(edge.fromNodeId) && !deletedIds.has(edge.toNodeId)),
    }));
    setSelectedId(null); setSelectedIds(new Set()); setSelectedConnectionId(null); setConnection(null);
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
  const focusNode = useCallback((id: string) => {
    const node = canvasRef.current.nodes.find((item) => item.id === id);
    if (!node) { setError('结果节点已移除，任务和视频仍保留在来源节点的记录中。'); return; }
    selectNode(id);
    requestAnimationFrame(() => {
      const container = containerRef.current;
      if (!container) return;
      const panel = container.querySelector<HTMLElement>(`[data-node-panel="${id}"]`);
      const toolbar = container.parentElement?.querySelector<HTMLElement>('[data-canvas-top-toolbar]');
      const viewport = focusViewportForNode(node, { width: container.clientWidth, height: container.clientHeight }, { above: (toolbar?.getBoundingClientRect().height || 54) + 28, below: panel?.getBoundingClientRect().height || 0, bottomInset: 64 });
      const current = canvasRef.current.viewport;
      if (current.x !== viewport.x || current.y !== viewport.y || current.k !== viewport.k) changeCanvas((value) => ({ ...value, viewport }), true);
    });
  }, [changeCanvas, selectNode]);
  function fitAllNodes() {
    const nodes = canvasRef.current.nodes;
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

  useEffect(() => {
    if (!user || !projectId) return;
    let active = true;
    getCloudProject(projectId).then((value) => { if (active) { acceptEnvelope(value); setSceneId((current) => current || value.project.graphData.nodes.find((node) => node.type === 'scene')?.id || ''); } }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    getWanSettings().then((value) => { if (active) setModel(value.model); }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
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
      canvasRef.current = value.canvas; savedCanvasRef.current = value.canvas; setCanvas(value.canvas); dirtyRef.current = false; setDirty(false); setInputDirty(false); setSelectedId(null); setSelectedIds(new Set()); setReferencePickerId(null); editVersion.current = 0; contentVersion.current = 0;
      const current = envelopeRef.current; if (current) acceptEnvelope({ ...current, revision: value.revision });
      window.history.replaceState(null, '', `${window.location.pathname}?id=${encodeURIComponent(projectId)}&sceneId=${encodeURIComponent(sceneId)}`);
    }).catch((problem: unknown) => { if (active) setError(errorText(problem)); }).finally(() => { if (active) setLoadingCanvas(false); });
    return () => { active = false; };
  }, [user, projectId, sceneId, acceptEnvelope]);
  useEffect(() => {
    if (!user || !projectId) return;
    let active = true;
    const refresh = () => getGenerations(projectId).then((value) => { if (active) setTasks([...value.generations].sort((a, b) => b.createdAt.localeCompare(a.createdAt))); }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    void refresh(); const timer = window.setInterval(() => { void refresh(); }, 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [user, projectId]);
  useEffect(() => {
    if (!user || !projectId || !sceneId) return;
    let active = true, refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      const result = await getTextGenerations(projectId);
      if (!active) return;
      setTextTasks([...result.textGenerations].sort((a, b) => b.createdAt.localeCompare(a.createdAt)));
      const applied = result.textGenerations.filter((task) => task.sceneId === sceneId && task.appliedRevision !== null && !syncedTextRevisions.current.has(`${task.id}:${task.appliedRevision}`));
      if (!applied.length) return;
      if (busyRef.current) return;
      const next = await getCloudProject(projectId);
      if (!active || busyRef.current || !mergeServerCanvas(next, sceneId)) return;
      if (dirtyRef.current) setMessage('文字结果与服务端版本已同步，本地编辑和视口调整继续保留，可继续保存画布。');
      applied.forEach((task) => syncedTextRevisions.current.add(`${task.id}:${task.appliedRevision}`));
    };
    const run = () => { void refresh().catch((problem: unknown) => { if (active) setError(errorText(problem)); }).finally(() => { refreshing = false; }); };
    run(); const timer = window.setInterval(run, 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [user, projectId, sceneId, mergeServerCanvas]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', guard); return () => window.removeEventListener('beforeunload', guard);
  }, [dirty]);
  useEffect(() => {
    if (!connection) return;
    const move = (event: MouseEvent) => setMouseWorld(screenToCanvas(event.clientX, event.clientY));
    const finish = (event: MouseEvent) => {
      const targetId = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-node-id]')?.dataset.nodeId;
      if (targetId) {
        const result = normalizeConnection(connection.nodeId, targetId, canvasRef.current.nodes, connection.handleType);
        if (result) changeCanvas((value) => ({ ...value, connections: value.connections.some((item) => item.fromNodeId === result.fromNodeId && item.toNodeId === result.toNodeId) ? value.connections : [...value.connections, { ...result, id: operationId() }] }));
      }
      setConnection(null);
    };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', finish);
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', finish); };
  }, [connection, changeCanvas, screenToCanvas]);

  async function saveCanvas() {
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
  useImperativeHandle(saveRef, () => ({ save: async () => {
    if (busyRef.current) throw new Error('素材操作正在执行，请稍后关闭。');
    if (dirtyRef.current) await saveCanvas();
  } }));
  async function perform(action: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(''); setMessage('');
    await action().catch((problem: unknown) => setError(errorText(problem))).finally(() => { busyRef.current = false; setBusy(false); });
  }
  function addNode(type: string, position?: Position) {
    if (!sceneId || loadingCanvas) return;
    const origin = position || screenToCanvas((containerRef.current?.getBoundingClientRect().left || 0) + 80, (containerRef.current?.getBoundingClientRect().top || 0) + 110);
    const node = makeNode(type, origin);
    if (!position) {
      let overlap = canvasRef.current.nodes.find((item) => item.type !== CanvasNodeType.Group && node.position.x < item.position.x + item.width + 28 && node.position.x + node.width + 28 > item.position.x && node.position.y < item.position.y + item.height + 28 && node.position.y + node.height + 28 > item.position.y);
      while (overlap) {
        node.position.x = overlap.position.x + overlap.width + 96;
        overlap = canvasRef.current.nodes.find((item) => item.type !== CanvasNodeType.Group && node.position.x < item.position.x + item.width + 28 && node.position.x + node.width + 28 > item.position.x && node.position.y < item.position.y + item.height + 28 && node.position.y + node.height + 28 > item.position.y);
      }
    }
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, node] })); selectNode(node.id); setCreatePosition(null); focusNode(node.id);
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
      const problem = generationInputProblem(source, displayNodes, canvasRef.current.connections);
      if (problem) throw new Error(problem);
      const prompt = rawPrompt(source), duration = Number(source.metadata?.seconds || 5), resolution = source.metadata?.vquality || '720P';
      const output = nextOutput(source, CanvasNodeType.Video);
      output.title = '视频结果'; output.metadata = { ...output.metadata, pendingResult: true };
      prepareOutput(source, output, { seconds: String(duration), vquality: resolution });
      await saveCanvas();
      const scene = envelopeRef.current?.project.graphData.nodes.find((node) => node.id === sceneId);
      const clipIds = new Set(scene?.data.timeline?.tracks.flatMap((track) => track.clips.map((clip) => clip.id)));
      const boundClip = tasks.find((task) => task.sceneId === sceneId && task.sourceNodeId === sourceNodeId && clipIds.has(task.clipId));
      const { generation } = await quoteGeneration(projectId, { sceneId, sourceNodeId, outputNodeId: output.id, clipId: boundClip?.clipId, prompt, duration, resolution, operationId: operationId() });
      selectNode(sourceNodeId); setTasks((current) => [generation, ...current.filter((item) => item.id !== generation.id)]); setMessage('报价已放在来源节点下方，请核对后确认费用。'); focusNode(sourceNodeId);
    });
  }
  async function generateFromConfig(sourceNodeId: string) {
    const source = canvasRef.current.nodes.find((node) => node.id === sourceNodeId);
    if (!source || source.type !== CanvasNodeType.Config) return;
    const kind = nodeDramaTask(source);
    if (kind.id === 'clip') { await requestQuote(source.id); return; }
    if (kind.kind === 'image') { setError('图像生成服务尚未配置。'); return; }
    await perform(async () => {
      if (kind.kind === 'text' && !textSettings?.configured) throw new Error('请由管理员配置 DeepSeek 文本模型 API。');
      if (kind.kind === 'text' && !textSettings?.canManage && !textSettings?.billingPolicyConfigured) throw new Error('平台销售计费规则尚未配置，文字生成尚未开放。');
      const problem = generationInputProblem(source, displayNodes, canvasRef.current.connections);
      if (problem) throw new Error(problem);
      const output = nextOutput(source, kind.kind === 'text' ? CanvasNodeType.Text : CanvasNodeType.Video);
      output.title = `${kind.label}结果`; output.metadata = { ...output.metadata, pendingResult: true };
      prepareOutput(source, output);
      const expectedRevision = await saveCanvas();
      if (kind.kind === 'text') {
        const { textGeneration } = await createTextGeneration(projectId, { sceneId, sourceNodeId, outputNodeId: output.id, task: kind.id, prompt: rawPrompt(source), expectedRevision, operationId: operationId() });
        setTextTasks((current) => [textGeneration, ...current.filter((task) => task.id !== textGeneration.id)]);
        setMessage('文字任务已保存，结果会出现在独立文字节点中。');
      } else {
        const { generation } = await joinGeneration(projectId, { sceneId, sourceNodeId, outputNodeId: output.id, expectedRevision, operationId: operationId() });
        setTasks((current) => [generation, ...current.filter((task) => task.id !== generation.id)]);
        setMessage('视频拼接任务已保存，由本机顺序处理。');
      }
      focusNode(source.id);
    });
  }
  function nextOutput(source: CanvasNodeData, type: CanvasNodeType) {
    const previous = canvasRef.current.nodes.find((node) => node.id === source.metadata?.outputNodeId && node.type === type && !node.metadata?.content && !node.metadata?.storageKey);
    const occupied = previous && (tasks.some((task) => task.outputNodeId === previous.id) || textTasks.some((task) => task.outputNodeId === previous.id));
    return previous && !occupied ? structuredClone(previous) : placeResult(source, canvasRef.current.nodes, type);
  }
  function prepareOutput(source: CanvasNodeData, output: CanvasNodeData, patch: Partial<CanvasNodeMetadata> = {}) {
    changeCanvas((value) => ({ ...value,
      nodes: [...value.nodes.filter((node) => node.id !== output.id).map((node) => node.id === source.id ? { ...node, metadata: { ...node.metadata, ...patch, outputNodeId: output.id } } : node), output],
      connections: value.connections.some((edge) => edge.fromNodeId === source.id && edge.toNodeId === output.id) ? value.connections : [...value.connections, { id: operationId(), fromNodeId: source.id, toNodeId: output.id }],
    }));
  }
  function expandPlot(task: TextGenerationView) {
    if (!task.result || !('shots' in task.result)) return;
    const anchor = canvasRef.current.nodes.find((node) => node.id === task.outputNodeId && node.type === CanvasNodeType.Text);
    if (!anchor) { setError('原文字结果节点已移除，请先核对画布。'); return; }
    const referenceIds = displayNodes.filter((node) => node.type === CanvasNodeType.Image && node.metadata?.storageKey && !node.metadata?.plotSpawnOf && ['角色', '场景', '道具'].some((label) => node.title.includes(label))).map((node) => node.id);
    const occupiedBottom = Math.max(anchor.position.y, ...canvasRef.current.nodes.filter((node) => node.id !== anchor.id).map((node) => node.position.y + node.height + 80));
    const graph = buildShotFrameGraph({ ...anchor, position: { ...anchor.position, y: occupiedBottom } }, task.result.shots, referenceIds);
    const existingIds = new Set(canvasRef.current.nodes.map((node) => node.id));
    const added = graph.nodes.filter((node) => !existingIds.has(node.id));
    if (!added.length) { setMessage('本次分镜流程已展开，现有节点和编辑继续保留。'); return; }
    const existingEdges = new Set(canvasRef.current.connections.map((edge) => `${edge.fromNodeId}:${edge.toNodeId}`));
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, ...added], connections: [...value.connections, ...graph.connections.filter((edge) => !existingEdges.has(`${edge.fromNodeId}:${edge.toNodeId}`))] }));
    selectNode(added[0].id); setMessage(`已展开 ${task.result.shots.length} 镜、${graph.videoCount} 段视频，按当前每段5秒计算共${graph.totalSeconds}秒。保存画布后可逐个制作；图像服务等待配置。`);
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
      await refreshSavedText(targetSceneId); setMessage(dirtyRef.current ? '文字结果已保存，请求期间新增的编辑继续保留。' : '文字结果已保存到原结果节点。');
    });
  }
  async function controlText(task: TextGenerationView, action: 'stop' | 'check') {
    await perform(async () => {
      const result = await (action === 'stop' ? stopTextGeneration(task.id) : getTextGeneration(task.id));
      setTextTasks((values) => values.map((item) => item.id === task.id ? result.textGeneration : item));
    });
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
      setTasks((values) => values.map((item) => item.id === task.id ? generation : item)); setMessage('已确认，任务将按顺序执行。');
    });
  }
  async function adopt(task: GenerationView) {
    await perform(async () => {
      if (dirty) await saveCanvas();
      const current = envelopeRef.current; if (!current) throw new Error('项目尚未加载。');
      const result = await adoptGeneration(task.id, current.revision); acceptEnvelope(result);
      const session = useProjectSessionStore.getState(); if (session.projectId === projectId && !session.dirty) session.loadProjectSnapshot(result.project);
      setMessage('视频已采用到任务绑定的场景与片段。');
    });
  }
  async function resume(task: GenerationView) { await perform(async () => { const result = await resumeGeneration(task.id); setTasks((values) => values.map((item) => item.id === task.id ? result.generation : item)); }); }
  async function upload(files: FileList | null) {
    const file = files?.[0]; if (!file) return;
    await perform(async () => {
      if (dirty) await saveCanvas();
      const asset = await uploadCloudAsset(projectId, file); acceptEnvelope(await getCloudProject(projectId));
      const type = asset.type === 'image' ? CanvasNodeType.Image : asset.type === 'video' ? CanvasNodeType.Video : asset.type === 'audio' ? CanvasNodeType.Audio : CanvasNodeType.Text;
      const target = canvasRef.current.nodes.find((node) => node.id === uploadTarget.current && node.type === type);
      const node = target || makeNode(type, { x: 60 + canvasRef.current.nodes.length * 30, y: 70 + canvasRef.current.nodes.length * 20 });
      const updated = { ...node, title: asset.name, metadata: { ...node.metadata, content: asset.type === 'text' ? String(asset.metadata?.content || '') : assetContentUrl(asset.id), storageKey: asset.id, status: 'success' as const } };
      changeCanvas((value) => ({ ...value, nodes: target ? value.nodes.map((item) => item.id === target.id ? updated : item) : [...value.nodes, updated] })); selectNode(node.id);
    });
    uploadTarget.current = null; if (fileInputRef.current) fileInputRef.current.value = '';
  }

  const sceneTasks = useMemo(() => tasks.filter((task) => task.sceneId === sceneId), [tasks, sceneId]);
  const displayNodes = canvas.nodes.filter((node) => node.metadata?.agentWorkspace !== 'project').map((node) => { const projected = projectCanvasNode(node, sceneTasks); return embedded ? materialDisplayNode(projected) : projected; });
  const scenes = envelope?.project.graphData.nodes.filter((node) => node.type === 'scene') || [];
  const agentBranches = useMemo(() => upstreamBranchEdges(envelope?.project.graphData.edges || [], sceneId), [envelope?.project.graphData.edges, sceneId]);
  const sceneTitle = (id: string) => { const scene = scenes.find((item) => item.id === id); return scene && 'title' in scene.data ? scene.data.title || '未命名场景' : '剧情场景'; };
  const selected = displayNodes.find((node) => node.id === selectedId);
  const disconnectReference = (fromNodeId: string, toNodeId: string) => changeCanvas((value) => ({ ...value, connections: value.connections.filter((item) => item.fromNodeId !== fromNodeId || item.toNodeId !== toNodeId) }));
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
  const nodeSelectCapture = (event: React.MouseEvent, id: string) => {
    if (event.button !== 0 || referencePickerId || tool === 'pan') return;
    const target = event.target instanceof Element ? event.target : null;
    const focusTarget = target?.closest<HTMLElement>('input,textarea,select,button,a[href],[contenteditable="true"],video,audio,[tabindex]');
    focusTarget?.focus({ preventScroll: true });
    if (event.shiftKey || event.metaKey || event.ctrlKey) { setSelectedIds((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }); setSelectedId(id); return; }
    selectNode(id);
  };
  const nodeMouseDown = (event: React.MouseEvent, id: string) => {
    if (event.button !== 0 || referencePickerId || tool === 'pan') return;
    event.stopPropagation();
    if (event.shiftKey || event.metaKey || event.ctrlKey) return;
    const node = canvasRef.current.nodes.find((item) => item.id === id); if (!node) return;
    const start = screenToCanvas(event.clientX, event.clientY);
    const startClient = { x: event.clientX, y: event.clientY };
    let moved = false;
    const moving = canvasRef.current.nodes.filter((item) => item.id === id || (node.type === CanvasNodeType.Group && item.metadata?.groupId === id));
    const move = (moveEvent: MouseEvent) => {
      if (!moved && Math.abs(moveEvent.clientX - startClient.x) <= 3 && Math.abs(moveEvent.clientY - startClient.y) <= 3) return;
      moved = true;
      const point = screenToCanvas(moveEvent.clientX, moveEvent.clientY);
      const next = canvasRef.current.nodes.map((item) => { const initial = moving.find((candidate) => candidate.id === item.id); const position = initial ? { x: initial.position.x + point.x - start.x, y: initial.position.y + point.y - start.y } : item.position; return position.x === item.position.x && position.y === item.position.y ? item : { ...item, position }; });
      if (next.some((item, index) => item !== canvasRef.current.nodes[index])) changeCanvas((value) => ({ ...value, nodes: next }));
    };
    const finish = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', finish); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', finish);
  };
  const regenerate = (id: string) => { const source = canvasRef.current.nodes.find((node) => node.id === id); if (source?.type === CanvasNodeType.Config) void generateFromConfig(id); else void requestQuote(id); };
  const retryNode = (node: CanvasNodeData) => { const task = sceneTasks.find((item) => item.outputNodeId === node.id || item.sourceNodeId === node.id); if (task?.remoteTaskId && ['query_interrupted', 'processing', 'downloading', 'submission_unknown'].includes(task.status)) void resume(task); else if (task?.status === 'failed') regenerate(task.sourceNodeId); else if (!task && (node.type === CanvasNodeType.Video || node.type === CanvasNodeType.Text)) createConfigForNode(node); };
  const renderNodePanel = (node: CanvasNodeData) => {
    if (embedded && node.metadata?.creativeArtifact) return <p>在“问问 AI”中选择成果并提出修改。</p>;
    const nodeTasks = sceneTasks.filter((task) => task.sourceNodeId === node.id || task.outputNodeId === node.id);
    const nodeTextTasks = textTasks.filter((task) => task.sceneId === sceneId && (task.sourceNodeId === node.id || task.outputNodeId === node.id));
    return <div data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()} onWheel={(event) => event.stopPropagation()}>
      {[CanvasNodeType.Text, CanvasNodeType.Video].includes(node.type as CanvasNodeType) && <button data-testid="generation-create-config" onClick={() => createConfigForNode(node)}>编辑生成配置</button>}
      {nodeTextTasks.map((task) => <TextGenerationTaskCard key={task.id} task={task} busy={busy || loadingCanvas} onStop={(value) => { void controlText(value, 'stop'); }} onResume={(value) => { void controlText(value, 'check'); }} onApply={(value) => { void applyText(value); }} onExpandPlot={expandPlot} onFocusOutput={focusNode} />)}
      {nodeTasks.map((task) => <GenerationTaskCard key={task.id} task={task} revision={envelope?.revision || 0} inputDirty={inputDirty} busy={busy || loadingCanvas} sceneTitle={sceneTitle(task.sceneId)} sourceAvailable={canvas.nodes.some((item) => item.id === task.sourceNodeId)} onConfirm={(value) => { void confirm(value); }} onRequote={regenerate} onAdopt={(value) => { void adopt(value); }} onResume={(value) => { void resume(value); }} onFocusOutput={focusNode} />)}
    </div>;
  };
  function groupSelection() { const bounds = getGroupWrapRect(collectGroupMemberNodes(selectedIds, canvas.nodes)); const group = { ...makeNode(CanvasNodeType.Group, { x: bounds.x, y: bounds.y }), width: bounds.width, height: bounds.height }; const result = applyGroupSelection(selectedIds, canvas.nodes, canvas.connections, group); if (result) { changeCanvas((value) => ({ ...value, nodes: result.nodes, connections: result.connections })); selectNode(group.id); } }
  function ungroupSelection() { const result = applyUngroupSelection(selectedIds, canvas.nodes, canvas.connections); if (result) { changeCanvas((value) => ({ ...value, nodes: result.nodes, connections: result.connections })); setSelectedId(null); setSelectedIds(new Set(result.selectedIds)); } }
  function duplicateSelection() {
    const current = canvasRef.current;
    const ids = new Set([...selectedIds, ...collectGroupMemberNodes(selectedIds, current.nodes).map((node) => node.id)]);
    const originals = current.nodes.filter((node) => ids.has(node.id)).map((node) => projectCanvasNode(node, sceneTasks));
    const idMap = new Map(originals.map((node) => [node.id, operationId()]));
    const remapReferences = (value?: string) => value?.replace(/@\[node:([^\]]+)\]/g, (reference, id: string) => idMap.has(id) ? `@[node:${idMap.get(id)}]` : reference);
    const copies = originals.map((node): CanvasNodeData => ({
      ...structuredClone(node), id: idMap.get(node.id)!, title: `${node.title} 副本`, position: { x: node.position.x + 36, y: node.position.y + 36 },
      metadata: { ...structuredClone(node.metadata), composerContent: remapReferences(node.metadata?.composerContent), prompt: remapReferences(node.metadata?.prompt), agentGoal: remapReferences(node.metadata?.agentGoal), agentTargetIds: node.metadata?.agentTargetIds?.map((id) => idMap.get(id) || id), groupId: node.metadata?.groupId ? idMap.get(node.metadata.groupId) : undefined, outputNodeId: node.metadata?.outputNodeId ? idMap.get(node.metadata.outputNodeId) : undefined, status: node.metadata?.storageKey || node.metadata?.content ? 'success' : 'idle', pendingResult: false, textGenerationId: undefined, videoTaskId: undefined, videoTaskProvider: undefined, errorDetails: undefined },
    }));
    const connections = current.connections.filter((edge) => idMap.has(edge.toNodeId)).map((edge) => ({ ...edge, id: operationId(), fromNodeId: idMap.get(edge.fromNodeId) || edge.fromNodeId, toNodeId: idMap.get(edge.toNodeId)! }));
    changeCanvas((value) => ({ ...value, nodes: [...value.nodes, ...copies], connections: [...value.connections, ...connections] }));
    setSelectedIds(new Set(copies.map((node) => node.id))); setSelectedId(selectedId ? idMap.get(selectedId) || copies[0]?.id || null : copies[0]?.id || null); setSelectedConnectionId(null);
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
  function renderConfigPanel(node: CanvasNodeData) {
    const inputs = buildNodeGenerationInputs(node.id, displayNodes, canvas.connections);
    const inputProblem = generationInputProblem(node, displayNodes, canvas.connections);
    const connectedNodes = displayNodes.filter((item) => canvas.connections.some((edge) => edge.fromNodeId === item.id && edge.toNodeId === node.id));
    const joinedVideos = [...new Map(connectedNodes.flatMap((item) => item.type === CanvasNodeType.Group ? displayNodes.filter((child) => child.metadata?.groupId === item.id && child.type === CanvasNodeType.Video) : item.type === CanvasNodeType.Video ? [item] : []).map((item) => [item.id, item])).values()];
    let joinedSeconds = 0, estimated = false, unknown = false;
    for (const video of joinedVideos) {
      const measured = mediaDurations[video.id];
      if (measured && measured.src === video.metadata?.content) { joinedSeconds += measured.seconds; continue; }
      if (video.metadata?.durationMs) { joinedSeconds += video.metadata.durationMs / 1000; continue; }
      const config = canvas.nodes.find((item) => item.type === CanvasNodeType.Config && nodeDramaTask(item).id === 'clip' && canvas.connections.some((edge) => edge.fromNodeId === item.id && edge.toNodeId === video.id));
      if (config) { joinedSeconds += Number(config.metadata?.seconds || 5); estimated = true; } else unknown = true;
    }
    const textServiceLabel = !textSettings ? '正在读取文本模型配置…' : !textSettings.configured ? 'DeepSeek 文本模型 API 尚未配置。' : !textSettings.canManage && !textSettings.billingPolicyConfigured ? '平台销售计费规则尚未配置，文字生成尚未开放。' : `DeepSeek · ${textSettings.model}。按实际 Token 用量记录供应商成本；平台销售计费尚未配置。`;
    return <CanvasConfigNodePanel node={node} nodes={displayNodes} connectedNodes={connectedNodes} inputs={inputs} model={model} textServiceLabel={textServiceLabel} textConnected={Boolean(textSettings?.configured && (textSettings.canManage || textSettings.billingPolicyConfigured))} busy={busy || loadingCanvas} canGenerate={!inputProblem} inputProblem={inputProblem} joinDuration={{ count: joinedVideos.length, seconds: unknown ? null : joinedSeconds, estimated }} onConfigChange={changeMetadata} onGenerate={(id) => { void generateFromConfig(id); }} onDisconnectReference={disconnectReference} onStartReferenceSelection={setReferencePickerId} />;
  }
  function renderCanvasNode(node: CanvasNodeData) {
    if (node.type === 'agent') return <AgentCanvasNode key={node.id} node={node} nodes={canvas.nodes} projectId={projectId} sceneId={sceneId}
      branchOptions={agentBranches.map((edge) => ({ id: edge.id, title: `${sceneTitle(edge.source)} → ${sceneTitle(edge.target)}` }))}
      selected={selectedIds.has(node.id)} disabled={busy || loadingCanvas} onSelect={nodeSelectCapture} onDrag={nodeMouseDown} onChange={changeMetadata} onDelete={(id) => removeNodes([id])}
      beforeRequest={async () => { if (dirtyRef.current) return saveCanvas(); const current = envelopeRef.current; if (!current) throw new Error('请先读取当前项目。'); return current.revision; }}
      refreshProject={async () => { await refreshSavedText(sceneId); }} onFocusSource={(id) => { if (canvasRef.current.nodes.some((item) => item.id === id)) focusNode(id); else if (scenes.some((item) => item.id === id)) void selectScene(id); }} />;
    const task = sceneTasks.find((item) => item.outputNodeId === node.id || (!item.outputNodeId && item.sourceNodeId === node.id && node.type === CanvasNodeType.Video));
    const retryable = task ? task.status === 'failed' || Boolean(task.remoteTaskId && ['query_interrupted', 'processing', 'downloading', 'submission_unknown'].includes(task.status)) : node.type === CanvasNodeType.Video && Boolean(rawPrompt(node));
    const referenceState = !referencePickerId ? undefined : referencePickerId === node.id ? 'target' : isCanvasReferenceNode(node, displayNodes) && !canvas.connections.some((edge) => edge.fromNodeId === node.id && edge.toNodeId === referencePickerId) ? 'available' : 'disabled';
    const panelWidth = Math.max(240, Math.min(600, canvasSize.width - 32));
    const nodeCenter = canvas.viewport.x + (node.position.x + node.width / 2) * canvas.viewport.k;
    const panelCenter = Math.max(panelWidth / 2 + 16, Math.min(canvasSize.width - panelWidth / 2 - 16, nodeCenter));
    return <CanvasNode
      key={node.id} data={node} scale={canvas.viewport.k}
      isSelected={selectedIds.has(node.id)}
      isRelated={hoveredId !== null && canvas.connections.some((edge) => edge.fromNodeId === hoveredId && edge.toNodeId === node.id || edge.toNodeId === hoveredId && edge.fromNodeId === node.id)}
      isFocusRelated={selectedId === node.id} isConnectionTarget={Boolean(connection && node.id !== connection.nodeId && mouseWorld.x >= node.position.x && mouseWorld.x <= node.position.x + node.width && mouseWorld.y >= node.position.y && mouseWorld.y <= node.position.y + node.height)}
      isConnecting={connection !== null} referenceSelectionState={referenceState}
      showPanel={selectedId === node.id && !referencePickerId && !resizing}
      panelWidth={panelWidth} panelOffsetX={(panelCenter - nodeCenter) / canvas.viewport.k} panelMaxHeight={Math.max(180, canvasSize.height * .42)}
      statusLabel={task ? taskLabels[task.status] : undefined} showImageInfo
      batchExpanded={expandedBatches.has(node.id)} groupChildCount={canvas.nodes.filter((item) => item.metadata?.groupId === node.id).length}
      mentionReferences={buildNodeMentionReferences(node, displayNodes, canvas.connections)}
      renderPanel={renderNodePanel} renderNodeContent={renderConfigPanel}
      onMouseDown={nodeMouseDown} onSelectCapture={nodeSelectCapture} onDelete={(id) => removeNodes([id])} deleteDisabled={busy || loadingCanvas} onHoverStart={setHoveredId} onHoverEnd={(id) => setHoveredId((current) => current === id ? null : current)}
      onConnectStart={(event, nodeId, handleType) => { event.preventDefault(); event.stopPropagation(); setConnection({ nodeId, handleType }); setMouseWorld(screenToCanvas(event.clientX, event.clientY)); }}
      onResizeStart={() => setResizing(true)} onResize={(id, width, height, position) => changeNode(id, { width, height, ...(position ? { position } : {}) })} onResizeEnd={() => setResizing(false)}
      onContentChange={(id, content) => { if (!(embedded && node.metadata?.creativeArtifact)) changeMetadata(id, { content }); }} onTitleChange={(id, title) => changeNode(id, { title })}
      onToggleBatch={(id) => setExpandedBatches((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; })}
      onSetBatchPrimary={setBatchPrimary} onDuplicateBatchImage={duplicateImage} onDownloadBatchImage={downloadImage}
      onDeleteBatchImage={(id, imageId) => { if (!window.confirm('移除此画布候选？原素材和生成任务继续保存。')) return; const original = canvasRef.current.nodes.find((item) => item.id === id); const images = original?.metadata?.images?.filter((item) => item.id !== imageId) || []; changeMetadata(id, { images, primaryImageId: images[0]?.id, content: images[0]?.content, storageKey: images[0]?.storageKey }); }}
      onRetry={retryable ? retryNode : undefined}
      onViewImage={(item, imageId) => { const image = item.metadata?.images?.find((candidate) => candidate.id === imageId); setPreview(image ? { ...item, metadata: { ...item.metadata, content: image.content, storageKey: image.storageKey } } : item); }}
      onSelectReference={(fromNodeId) => { if (referencePickerId) { changeCanvas((value) => ({ ...value, connections: [...value.connections, { id: operationId(), fromNodeId, toNodeId: referencePickerId }] })); setReferencePickerId(null); } }}
      onContextMenu={(event, id) => { event.preventDefault(); selectNode(id); }}
    />;
  }

  if (!user) return <main className="fmv-loading">{userError || '正在读取账号…'}</main>;
  if (!projectId) return <WorkspaceShell user={user} title="剧情画布"><p className="fmv-error">缺少项目，请从项目列表打开。</p></WorkspaceShell>;
  const content = <>
    <div className="fmv-studio-toolbar">{embedded ? <label className="flex items-center gap-2 text-sm">所属场景<select data-testid="material-scene-select" className="rounded border border-white/20 bg-[#24211d] p-2" value={sceneId} disabled={busy || loadingCanvas} onChange={(event) => { void selectScene(event.target.value); }}>{scenes.map((scene) => <option key={scene.id} value={scene.id}>{sceneTitle(scene.id)}</option>)}</select></label> : <nav className="fmv-studio-links" aria-label="项目创作入口"><Link href={`/zh-CN/editor?id=${encodeURIComponent(projectId)}`}>故事总画布</Link><span>剧情素材画布</span><Link href={`/zh-CN/nodes?id=${encodeURIComponent(projectId)}`}>场景时间线</Link><Link href="/zh-CN/assets">素材库</Link></nav>}<div className="flex items-center gap-3"><span role="status" data-testid="save-status" className="fmv-save-state">{loadingCanvas ? '正在读取画布' : dirty ? inputDirty ? '有未保存的内容或连线' : '视口有未保存的调整' : `已保存 · 版本 ${envelope?.revision ?? '…'}`}</span><button data-testid="canvas-save" className="fmv-save-button flex items-center gap-2" disabled={busy || loadingCanvas || !sceneId} onClick={() => { void perform(async () => { await saveCanvas(); }); }}><Save size={15} />保存画布</button></div></div>
    {error && <p role="alert" className="fmv-error fmv-studio-error">{error}</p>}{message && <p role="status" className="fmv-stage-note">{message}</p>}
    <div className="fmv-studio-body">
      {!embedded && <aside className="fmv-scenes"><h2>剧情场景</h2>{scenes.map((scene) => <button key={scene.id} data-active={scene.id === sceneId} data-testid="scene-item" disabled={busy} onClick={() => { void selectScene(scene.id); }}><Film size={14} />{sceneTitle(scene.id)}</button>)}{scenes.length === 0 && <p>创建剧情场景后开始组织素材。</p>}<form onSubmit={(event) => { void createScene(event); }}><label>新场景名称<input data-testid="scene-title" value={newSceneTitle} onChange={(event) => setNewSceneTitle(event.target.value)} required maxLength={120} /></label><button data-testid="scene-create" disabled={busy || !envelope}>创建场景</button></form></aside>}
      <section className="fmv-canvas-column" aria-label="剧情素材画布"><CanvasProviders>
        <div inert={loadingCanvas} onLoadedMetadataCapture={(event) => {
          const media = event.target;
          if (!(media instanceof HTMLVideoElement) || !media.dataset.canvasVideo || !Number.isFinite(media.duration)) return;
          const id = media.dataset.canvasVideo, src = media.getAttribute('src') || '', seconds = media.duration;
          setMediaDurations((current) => current[id]?.src === src && current[id]?.seconds === seconds ? current : { ...current, [id]: { src, seconds } });
        }} style={{ height: '100%', '--canvas-panel': theme.node.panel, '--canvas-fill': theme.node.fill, '--canvas-border': theme.node.stroke, '--canvas-text': theme.node.text, '--canvas-toolbar': theme.toolbar.panel, '--canvas-hover': theme.toolbar.itemHover } as React.CSSProperties}>
          <div className="fmv-canvas-controls" data-canvas-no-zoom data-canvas-top-toolbar>
            <button data-testid="canvas-add-text" disabled={!sceneId || loadingCanvas} onClick={() => addNode(CanvasNodeType.Text)}><FileText size={15} />文字</button>
            <button data-testid="canvas-add-config" disabled={!sceneId || loadingCanvas} onClick={() => addNode(CanvasNodeType.Config)}><Settings2 size={15} />生成</button>
            <button data-testid="canvas-add-agent" disabled={!sceneId || loadingCanvas} onClick={() => addNode('agent')}><Bot size={15} />创作助手</button>
            <button data-testid="canvas-add-video" disabled={!sceneId || loadingCanvas} onClick={() => addNode(CanvasNodeType.Video)}><Film size={15} />视频</button>
            <button onClick={() => { uploadTarget.current = null; fileInputRef.current?.click(); }} disabled={!sceneId || busy || loadingCanvas}><Upload size={15} />导入素材</button>
            <button aria-label={tool === 'select' ? '切换平移工具' : '切换选择工具'} onClick={() => setTool(tool === 'select' ? 'pan' : 'select')}>{tool === 'select' ? <Hand size={15} /> : <MousePointer2 size={15} />}</button>
            <CanvasZoomControls scale={canvas.viewport.k} onScaleChange={(k) => changeCanvas((value) => ({ ...value, viewport: { ...value.viewport, k } }), true)} onReset={fitAllNodes} isMiniMapOpen={miniMapOpen} onToggleMiniMap={() => setMiniMapOpen((value) => !value)} className="fmv-canvas-zoom-controls" />
            <input ref={fileInputRef} data-testid="canvas-import-file" type="file" accept="image/*,video/*,audio/*,.txt,.md" hidden onChange={(event) => { void upload(event.target.files); }} />
          </div>
          <InfiniteCanvas containerRef={containerRef} viewport={canvas.viewport} tool={tool} onViewportChange={(viewport) => { if (!loadingCanvas && sceneId) changeCanvas((value) => ({ ...value, viewport }), true); }} onCanvasMouseDown={() => { if (!referencePickerId) { setSelectedId(null); setSelectedIds(new Set()); setSelectedConnectionId(null); setCreatePosition(null); } }} onCanvasDeselect={() => { if (!referencePickerId) { setSelectedId(null); setSelectedIds(new Set()); setSelectedConnectionId(null); setCreatePosition(null); } }} onCanvasDoubleClick={(event) => { if (!referencePickerId) setCreatePosition(screenToCanvas(event.clientX, event.clientY)); }}>
            <svg className="pointer-events-none absolute left-0 top-0 h-[10000px] w-[10000px] overflow-visible" style={{ zIndex: 0 }}><g>{canvas.connections.map((edge) => { const from = displayNodes.find((node) => node.id === edge.fromNodeId), to = displayNodes.find((node) => node.id === edge.toNodeId); return from && to ? <ConnectionPath key={edge.id} connection={edge} from={from} to={to} active={selectedConnectionId === edge.id} onSelect={() => { setSelectedConnectionId(edge.id); setSelectedId(null); setSelectedIds(new Set()); }} /> : null; })}{connection && <ActiveConnectionPath node={displayNodes.find((node) => node.id === connection.nodeId)} handle={connection} mouseWorld={mouseWorld} />}</g></svg>
            {displayNodes.map(renderCanvasNode)}
            {createPosition && <NodeCreateMenu position={createPosition} scale={canvas.viewport.k} onCreate={(type) => addNode(type, createPosition)} onClose={() => setCreatePosition(null)} />}
          </InfiniteCanvas>
          {miniMapOpen && <Minimap nodes={displayNodes} viewport={canvas.viewport} viewportSize={canvasSize} onViewportChange={(viewport) => changeCanvas((value) => ({ ...value, viewport }), true)} />}
          {canvas.nodes.length === 0 && <div className="fmv-node-empty-hint">添加文字与生成节点，在节点中连接参考、编写要求并查看报价。<br />双击画布可创建节点。</div>}
          {referencePickerId && <button className="fmv-node-reference-selection" onClick={() => setReferencePickerId(null)}>点击画布节点添加参考 · 完成选择</button>}
          {selectedConnectionId && <div className="fmv-node-actions"><button onClick={() => { changeCanvas((value) => ({ ...value, connections: value.connections.filter((edge) => edge.id !== selectedConnectionId) })); setSelectedConnectionId(null); }}><Trash2 size={14} />断开连接</button></div>}
          {selected && !referencePickerId && <div className="fmv-node-actions" data-testid="canvas-node-actions" data-canvas-no-zoom>
            <button onClick={() => focusNode(selected.id)}>聚焦节点</button>
            <button data-testid="canvas-copy-selection" onClick={duplicateSelection}><Copy size={14} />复制</button>
            <button onClick={() => { uploadTarget.current = selected.id; fileInputRef.current?.click(); }}><Upload size={14} />导入素材</button>
            {canGroupSelectedNodes(selectedIds, canvas.nodes) && <button data-testid="canvas-group-selection" onClick={groupSelection}><Group size={14} />组合</button>}
            {canUngroupSelectedNodes(selectedIds, canvas.nodes) && <button data-testid="canvas-ungroup-selection" onClick={ungroupSelection}><Ungroup size={14} />取消组合</button>}
            {selected.metadata?.storageKey && <a href={assetContentUrl(selected.metadata.storageKey)} download={selected.title}>下载素材</a>}
            {selected.metadata?.storageKey && ['image', 'video', 'audio'].includes(selected.type) && <button data-testid="canvas-adopt-to-timeline" disabled={busy || loadingCanvas} onClick={() => { void perform(async () => {
              if (dirtyRef.current) await saveCanvas();
              const current = await getCloudProject(projectId);
              const saved = await addAssetToSceneTimeline(projectId, sceneId, selected.metadata!.storageKey!, current.revision);
              acceptEnvelope(saved); setMessage('素材已加入当前场景时间线，可在故事总画布中剪辑和设置互动。');
            }); }}>加入场景时间线</button>}
            {selected.type === CanvasNodeType.Image && selected.metadata?.storageKey && <button data-testid="canvas-image-split" disabled={busy} onClick={() => setSplitNode(selected)}><Grid2x2 size={14} />切片</button>}
            <button data-testid="canvas-delete-selection" disabled={busy || loadingCanvas} onClick={() => removeNodes([...selectedIds])}><Trash2 size={14} />删除</button>
          </div>}
          <Modal title={preview?.title || '素材预览'} open={preview !== null} footer={null} onCancel={() => setPreview(null)} width={960}>{preview && <img src={preview.metadata?.storageKey ? assetContentUrl(preview.metadata.storageKey) : preview.metadata?.content} alt={preview.title} style={{ width: '100%', maxHeight: '75dvh', objectFit: 'contain' }} />}</Modal>
          <CanvasNodeSplitDialog key={splitNode?.id || 'closed'} dataUrl={splitNode?.metadata?.storageKey ? assetContentUrl(splitNode.metadata.storageKey) : ''} open={splitNode !== null} busy={busy} onClose={() => { if (!busy) setSplitNode(null); }} onConfirm={(params) => { void splitImage(params); }} />
        </div>
      </CanvasProviders></section>
    </div>
  </>;
  return embedded ? <div className="fmv-creation-workspace fmv-material-embedded" data-testid="embedded-material-canvas">{content}</div> : <WorkspaceShell user={user} title={envelope?.project.title || '正在加载项目…'} canvas>{content}</WorkspaceShell>;
}
