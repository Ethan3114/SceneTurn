'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ViewportPortal, useReactFlow } from '@xyflow/react';
import type { CanvasNodeData, CanvasNodeMetadata } from '@/app/_features/creation-canvas/upstream/canvas';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { getCloudCanvas, getCloudProject, operationId, saveCloudCanvas } from '@/app/_utils/cloudApi';
import { mergeProjectChanges } from '@/app/_utils/mergeProjectChanges';
import AgentCanvasNode from '../workbench/AgentCanvasNode';

export default function ProjectAgentCanvas({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  const [node, setNode] = useState<CanvasNodeData | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const nodeRef = useRef(node);
  const refreshRef = useRef<Promise<void> | null>(null);
  const anchorRef = useRef('');
  const { screenToFlowPosition, setCenter, getZoom } = useReactFlow();
  const scenes = useProjectSessionStore((state) => state.nodes).filter((item) => item.type === 'scene');
  useEffect(() => { nodeRef.current = node; }, [node]);
  const refresh = useCallback(async () => {
    if (refreshRef.current) return refreshRef.current;
    const work = async () => {
      const remote = await getCloudProject(projectId);
      const state = useProjectSessionStore.getState();
      if (state.projectId !== projectId) return;
      if (state.dirty && state.project) {
        const local = { ...state.project, title: state.title, graphData: state.getGraphSnapshot(), assets: state.assets };
        const merged = mergeProjectChanges(state.project, local, remote.project);
        state.loadProjectSnapshot(merged.project);
        useProjectSessionStore.setState({ project: remote.project, dirty: true, revision: state.revision + 1, savedRevision: state.savedRevision });
        if (merged.conflicts.length) setNotice('相同字段采用你的手动编辑，Agent结果与历史继续保存在项目中。请核对后保存。');
      } else state.loadProjectSnapshot(remote.project);
    };
    refreshRef.current = work().finally(() => { refreshRef.current = null; });
    return refreshRef.current;
  }, [projectId]);
  async function show() {
    setError('');
    const anchor = scenes[0];
    if (!anchor) { setError('请先在总画布创建场景。'); return; }
    anchorRef.current = anchor.id;
    const saved = await getCloudCanvas(projectId, anchor.id);
    const existing = saved.canvas.nodes.find((item) => item.type === 'agent' && item.metadata?.agentWorkspace === 'project');
    const position = screenToFlowPosition({ x: Math.min(window.innerWidth - 340, 530), y: 180 });
    const value: CanvasNodeData = existing || { id: operationId(), type: 'agent', title: '故事与剪辑助手', position, width: 440, height: 700,
      metadata: { agentWorkspace: 'project', agentRole: 'director', agentDeliveryMode: 'tool_action', agentExecutor: 'platform', agentGoal: '', agentTargetIds: scenes.filter((item) => item.selected).map((item) => item.id) } };
    setNode(value); nodeRef.current = value; setOpen(true);
    if (existing) void setCenter(existing.position.x + existing.width / 2, existing.position.y + existing.height / 2, { zoom: Math.min(getZoom(), 0.85), duration: 200 });
  }
  async function beforeRequest() {
    await refresh();
    const state = useProjectSessionStore.getState();
    if (state.dirty) await state.saveNow();
    if (useProjectSessionStore.getState().dirty) throw new Error('项目编辑尚未保存，请核对保存提示。');
    const value = nodeRef.current;
    if (!value) throw new Error('请重新打开故事与剪辑助手。');
    const saved = await getCloudCanvas(projectId, anchorRef.current);
    const previous = saved.canvas.nodes.find((item) => item.id === value.id);
    if (!previous || (['title', 'width', 'height', 'position', 'metadata'] as const).some((key) => JSON.stringify(previous[key]) !== JSON.stringify(value[key]))) {
      const stored = await saveCloudCanvas(projectId, { ...saved.canvas, nodes: [...saved.canvas.nodes.filter((item) => item.id !== value.id), value] }, saved.revision);
      const persisted = stored.canvas.nodes.find((item) => item.id === value.id);
      if (persisted && nodeRef.current === value) { nodeRef.current = persisted; setNode(persisted); }
    }
    await refresh();
    const revision = useProjectSessionStore.getState().project?.metadata.serverRevision;
    if (!revision) throw new Error('项目版本尚未读取完成。');
    return revision;
  }
  function change(id: string, patch: Partial<CanvasNodeMetadata>) {
    setNode((current) => { if (!current || current.id !== id) return current; const value = { ...current, metadata: { ...current.metadata, ...patch } }; nodeRef.current = value; return value; });
  }
  function drag(event: React.MouseEvent) {
    if (event.button !== 0 || (event.target as HTMLElement).closest('button')) return;
    event.preventDefault(); event.stopPropagation();
    const start = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    const origin = nodeRef.current?.position;
    if (!origin) return;
    const move = (next: MouseEvent) => {
      const point = screenToFlowPosition({ x: next.clientX, y: next.clientY });
      setNode((current) => { if (!current) return current; const value = { ...current, position: { x: origin.x + point.x - start.x, y: origin.y + point.y - start.y } }; nodeRef.current = value; return value; });
    };
    const end = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', end); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', end);
  }
  return <>
    <div className="nodrag nopan absolute left-4 top-20 z-20 max-w-[calc(100%-2rem)] rounded-xl border border-white/15 bg-[#24211d] p-2 text-sm text-white shadow-xl">
      <button data-testid="project-agent-open" className="px-3 py-2" onClick={() => { void show().catch((problem: Error) => setError(problem.message)); }}>故事与剪辑助手</button>
      {open && <button data-testid="project-agent-save" className="px-3 py-2" onClick={() => { void beforeRequest().then(() => setNotice('助手设置已保存。')).catch((problem: Error) => setError(problem.message)); }}>保存助手</button>}
      {error && <p role="alert">{error}</p>}{notice && <p role="status" className="max-w-sm px-3 text-xs">{notice}</p>}
    </div>
    {open && node && <ViewportPortal><div className="nodrag nopan nowheel" style={{ pointerEvents: 'all' }}>
      <AgentCanvasNode node={node} nodes={scenes.map((item) => ({ id: item.id, type: 'text', title: String(item.data.title || '场景'), position: item.position, width: 300, height: 200, metadata: {} }))}
        projectId={projectId} sceneId={anchorRef.current} workspace="project" branchOptions={[]} selected disabled={false}
        onSelect={() => undefined} onDrag={drag} onChange={change} onDelete={() => { void beforeRequest().then(() => setOpen(false)).catch((problem: Error) => setError(problem.message)); }} beforeRequest={beforeRequest}
        refreshProject={refresh} onFocusSource={(id) => { const target = scenes.find((item) => item.id === id); if (target) void setCenter(target.position.x, target.position.y, { zoom: 1, duration: 200 }); }} />
    </div></ViewportPortal>}
  </>;
}
