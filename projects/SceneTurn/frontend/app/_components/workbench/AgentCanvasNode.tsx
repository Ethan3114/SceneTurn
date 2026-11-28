'use client';

import React, { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Bot, ChevronDown, Link2, Play, Square, Trash2 } from 'lucide-react';

import type { CanvasNodeData, CanvasNodeMetadata } from '@/app/_features/creation-canvas/upstream/canvas';
import { useTaskPolling } from '@/app/_hooks/useTaskPolling';
import { agentErrorMessage, agentStatuses, agentToolTitles, controlAgentRun, createAgentRun, createCodexPairing, createProjectMemory, getAgentRoles, getAgentRuns, getCodexConnections, getProjectMemories, readMemorySource, revokeCodexConnection, type AgentRole, type AgentRun, type CodexConnection, type ProjectMemory } from '@/app/_utils/agentApi';
import { errorText } from './WorkspaceShell';
import './agent-workbench.css';

interface Props {
  node: CanvasNodeData;
  nodes: CanvasNodeData[];
  projectId: string;
  sceneId: string;
  workspace?: 'project' | 'scene';
  branchOptions: { id: string; title: string }[];
  selected: boolean;
  disabled: boolean;
  onSelect: (event: React.MouseEvent, id: string) => void;
  onDrag?: (event: React.MouseEvent, id: string) => void;
  onChange: (id: string, patch: Partial<CanvasNodeMetadata>) => void;
  onDelete: (id: string) => void;
  beforeRequest: () => Promise<number>;
  refreshProject: () => Promise<void>;
  onFocusSource: (id: string) => void;
}
const activeStatuses = new Set(['queued', 'running', 'stopping']);
const memoryKinds: Record<string, string> = { setting: '项目设定', decision: '本人决定', story_event: '剧情事件', creation_event: '创作记录', candidate: '候选内容', summary: '来源摘要' };
const deliveryTitles = { professional: '完整专业产物', text_edit: '局部文字修订', tool_action: '执行工具操作', legacy: '已有任务' };
function completionText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    for (const key of ['summary', 'message', 'text', 'finalText', 'bodyText', 'content']) if (key in value && typeof (value as Record<string, unknown>)[key] === 'string') return (value as Record<string, string>)[key];
  }
  return '';
}

function AgentCanvasNode({ node, nodes, projectId, sceneId, workspace = 'scene', branchOptions, selected, disabled, onSelect, onDrag, onChange, onDelete, beforeRequest, refreshProject, onFocusSource }: Props) {
  const [roles, setRoles] = useState<AgentRole[]>([]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [memories, setMemories] = useState<ProjectMemory[]>([]);
  const [connections, setConnections] = useState<CodexConnection[]>([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState('');
  const [memoryDraft, setMemoryDraft] = useState('');
  const [memoryKind, setMemoryKind] = useState('setting');
  const [pairing, setPairing] = useState<Awaited<ReturnType<typeof createCodexPairing>> | null>(null);
  const [sourcePreview, setSourcePreview] = useState<Awaited<ReturnType<typeof readMemorySource>> | null>(null);
  const revisionSeen = useRef(new Map<string, number>());
  const refreshSequence = useRef(0);
  const actionsRef = useRef({ refreshProject });
  useEffect(() => { actionsRef.current = { refreshProject }; }, [refreshProject]);
  const roleId = roles.some((item) => item.id === node.metadata?.agentRole) ? node.metadata!.agentRole! : roles[0]?.id || (workspace === 'project' ? 'director' : 'writer');
  const role = roles.find((item) => item.id === roleId);
  const goal = node.metadata?.agentGoal || '';
  const executor = node.metadata?.agentExecutor || 'platform';
  const deliveryMode = workspace === 'project' ? 'tool_action' : node.metadata?.agentDeliveryMode || 'professional';
  const deliveryUnavailable = deliveryMode === 'text_edit' && role != null && !role.tools.includes('write_script');
  const branchId = node.metadata?.agentBranchId || '';
  const targetIds = node.metadata?.agentTargetIds || [];
  const sceneRuns = runs.filter((item) => (item.workspace === 'project') === (workspace === 'project') && item.sceneId === sceneId && (!item.controllerNodeId || item.controllerNodeId === node.id || !nodes.some((candidate) => candidate.id === item.controllerNodeId)));
  const run = sceneRuns.find((item) => item.id === selectedRunId) || sceneRuns[0];
  const executing = run && activeStatuses.has(run.status);
  const waitingForCodex = sceneRuns.some((item) => item.executor === 'codex' && item.status === 'waiting_connection');
  const pollingRuns = sceneRuns.some((item) => activeStatuses.has(item.status)) || (waitingForCodex && connections.some((item) => item.projectId === projectId && item.status === 'online'));
  const reload = useCallback(async () => {
    const request = ++refreshSequence.current;
    try {
      const [nextRuns, nextConnections, nextRoles] = await Promise.all([getAgentRuns(projectId), getCodexConnections(projectId), getAgentRoles(projectId, workspace)]);
      if (request !== refreshSequence.current) return;
      setRuns(nextRuns); setConnections(nextConnections); setRoles(nextRoles); setLoadError('');
      for (const value of nextRuns) {
        const revision = value.currentRevision ?? value.projectRevision;
        if (revisionSeen.current.get(value.id) === revision) continue;
        if (value.sceneId === sceneId && (value.workspace === 'project') === (workspace === 'project') && value.steps.length) await actionsRef.current.refreshProject();
        if (request !== refreshSequence.current) return;
        revisionSeen.current.set(value.id, revision);
      }
    } catch (problem: unknown) {
      if (request === refreshSequence.current) setLoadError(errorText(problem));
    }
  }, [projectId, sceneId, workspace]);
  useEffect(() => {
    void reload();
    return () => { refreshSequence.current += 1; };
  }, [reload]);
  useTaskPolling(pollingRuns, 2500, reload);
  useEffect(() => {
    if (pollingRuns || (!waitingForCodex && !pairing)) return;
    const refreshConnection = () => { void reload(); };
    window.addEventListener('focus', refreshConnection);
    return () => window.removeEventListener('focus', refreshConnection);
  }, [pairing, pollingRuns, reload, waitingForCodex]);
  async function perform(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    await action().catch((problem: unknown) => setError(errorText(problem))).finally(() => setBusy(false));
  }
  function change(patch: Partial<CanvasNodeMetadata>) { onChange(node.id, patch); }
  async function start(event: React.FormEvent) {
    event.preventDefault();
    await perform(async () => {
      const expectedRevision = await beforeRequest();
      const value = await createAgentRun(projectId, { controllerNodeId: node.id, sceneId, workspace, ...(branchId && workspace === 'scene' ? { branchId } : {}), role: roleId, executor, deliveryMode, goal, targetIds, expectedRevision });
      setSelectedRunId(value.id); setRuns((items) => [value, ...items.filter((item) => item.id !== value.id)]);
      setNotice('任务已保存，可以继续编辑画布。');
      void reload();
    });
  }
  async function control(action: 'stop' | 'resume') {
    if (!run) return;
    await perform(async () => {
      const expectedRevision = action === 'resume' ? await beforeRequest() : run.currentRevision ?? run.projectRevision;
      const value = await controlAgentRun(projectId, run.id, action, expectedRevision);
      setRuns((items) => items.map((item) => item.id === value.id ? value : item));
      setNotice(action === 'stop' ? '停止请求已保存，已提交任务继续保留真实状态。' : '已按当前项目版本请求继续。');
      void reload();
    });
  }
  async function readMemories() { await perform(async () => { setMemories(await getProjectMemories(projectId)); }); }
  async function saveMemory(event: React.FormEvent) {
    event.preventDefault();
    await perform(async () => {
      const expectedRevision = await beforeRequest();
      const sources = memoryKind === 'setting' || memoryKind === 'decision' ? [{ kind: 'project', id: projectId, revision: expectedRevision }] : targetIds.length ? targetIds.map((id) => ({ kind: workspace === 'project' ? 'scene' : 'canvas_node', id, revision: expectedRevision, sceneId: workspace === 'project' ? id : sceneId })) : [{ kind: 'scene', id: sceneId, revision: expectedRevision, sceneId }];
      const globalMemory = memoryKind === 'setting' || memoryKind === 'decision';
      await createProjectMemory(projectId, { ...(globalMemory ? {} : { sceneId, ...(branchId ? { branchId } : {}) }), kind: memoryKind, content: memoryDraft, expectedRevision, sources });
      await refreshProject(); setMemories(await getProjectMemories(projectId)); setMemoryDraft(''); setNotice('设定已保存，后续任务会按来源和分支读取。');
    });
  }
  return <article className="fmv-agent-node" data-testid="canvas-node" data-agent-node data-node-id={node.id} data-node-type="agent" data-selected={selected} tabIndex={0}
    style={{ left: node.position.x, top: node.position.y, width: node.width, height: node.height }}
    onMouseDownCapture={(event) => { if (onDrag || !(event.target instanceof Element && event.target.closest('[data-canvas-drag-node]'))) onSelect(event, node.id); }} onMouseDown={(event) => event.stopPropagation()} onWheel={(event) => event.stopPropagation()}>
    <header data-testid="canvas-node-header" data-canvas-drag-node={onDrag ? undefined : node.id} onMouseDown={(event) => onDrag?.(event, node.id)}><span><Bot size={18} />{workspace === 'project' ? '故事与剪辑助手' : '素材创作助手'}</span><button type="button" aria-label={workspace === 'project' ? '收起故事与剪辑助手' : '删除创作助手节点'} data-testid="canvas-node-delete" onMouseDown={(event) => event.stopPropagation()} onClick={() => onDelete(node.id)}><Trash2 size={16} /></button></header>
    <div className="fmv-agent-scroll" data-testid="canvas-node-body">
      <form onSubmit={(event) => { void start(event); }}>
        <div className="fmv-agent-fields"><label>角色<select data-testid="agent-role" value={roleId} disabled={busy || executing} onChange={(event) => change({ agentRole: event.target.value })}>{roles.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label><label>执行方式<select data-testid="agent-executor" value={executor} disabled={busy || executing} onChange={(event) => change({ agentExecutor: event.target.value as 'platform' | 'codex' })}><option value="platform">平台 API · DeepSeek Flash</option><option value="codex">本人 Codex</option></select></label></div>
        {role && <p className="fmv-agent-muted">{role.description}</p>}
        {workspace === 'scene' && <label>交付类型<select data-testid="agent-delivery-mode" value={deliveryMode} disabled={busy || executing} onChange={(event) => change({ agentDeliveryMode: event.target.value as 'professional' | 'text_edit' | 'tool_action' })}><option value="professional">完整专业产物</option><option value="text_edit" disabled={role != null && !role.tools.includes('write_script')}>局部文字修订</option><option value="tool_action">执行工具操作</option></select></label>}
        {deliveryUnavailable && <p className="fmv-agent-muted">该角色未开放局部文字修订，请选择完整专业产物或执行工具操作。</p>}
        <label>本次任务<textarea data-testid="agent-goal" value={goal} required maxLength={10000} rows={4} onChange={(event) => change({ agentGoal: event.target.value })} placeholder="描述希望完成的剧情任务和具体要求" /></label>
        {workspace === 'scene' && <label>剧情分支<select data-testid="agent-branch" value={branchId} disabled={busy || executing} onChange={(event) => change({ agentBranchId: event.target.value })}><option value="">当前场景的有效历史</option>{branchOptions.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>}
        <details open={workspace === 'project'}><summary>{workspace === 'project' ? '允许修改的场景' : '允许修改的现有节点'} · {targetIds.length}个</summary><p className="fmv-agent-muted">{workspace === 'project' ? '选择本次允许修改剧情、时间线或互动的场景。' : '勾选本次允许修改的目标。未选择目标时，任务仅能创建新内容。'}</p><div className="fmv-agent-targets">{nodes.filter((item) => item.type !== 'agent' && item.type !== 'group').map((item) => <label key={item.id}><input type="checkbox" data-testid="agent-target" value={item.id} checked={targetIds.includes(item.id)} onChange={(event) => change({ agentTargetIds: event.target.checked ? [...targetIds, item.id] : targetIds.filter((id) => id !== item.id) })} />{item.title}</label>)}</div></details>
        <p className="fmv-agent-muted">平台任务最多{workspace === 'project' ? 24 : 12}次模型请求，单次响应最多32768 Token，任务费用最高¥10.00。图片与视频另行报价并逐次确认。</p>
        {(role?.reason || role?.available === false) && <p className="fmv-agent-muted">{role.reason || '该角色所需工具尚未配置。'}</p>}
        <button className="fmv-agent-primary" data-testid="agent-start" disabled={disabled || busy || executing || deliveryUnavailable || !goal.trim() || !roles.length || role?.available === false}><Play size={14} />开始任务</button>
      </form>
      {error && <p role="alert" className="fmv-agent-error">{error}</p>}{loadError && <p role="alert" className="fmv-agent-error">{loadError}</p>}{notice && <p role="status" className="fmv-agent-notice">{notice}</p>}
      {sceneRuns.length > 0 && <section className="fmv-agent-run"><label>任务记录<select data-testid="agent-run-select" value={run?.id || ''} onChange={(event) => setSelectedRunId(event.target.value)}>{sceneRuns.map((item) => <option key={item.id} value={item.id}>{agentStatuses[item.status] || item.status} · {item.goal.slice(0, 35)}</option>)}</select></label>{run && <>
        <p className="fmv-agent-state" data-testid="agent-status" role="status">{agentStatuses[run.status] || run.status}</p>
        {run.deliveryMode && <p className="fmv-agent-muted">交付：{deliveryTitles[run.deliveryMode]}</p>}
        {completionText(run.result) && <p className="fmv-agent-result" data-testid="agent-result">{completionText(run.result)}</p>}
        {agentErrorMessage(run.error) && <p className="fmv-agent-error">{agentErrorMessage(run.error)}</p>}
        <ol className="fmv-agent-steps">{run.steps.map((step, index) => <li key={step.id || index}><span>{step.title || agentToolTitles[step.toolName || step.tool || ''] || `创作步骤 ${index + 1}`}</span><small>{agentStatuses[step.status || ''] || step.status || ''}</small></li>)}</ol>
        <p className="fmv-agent-muted" data-testid="agent-usage">{run.executor === 'platform' ? `API用量：${run.usage.totalTokens == null ? '待返回' : `${run.usage.totalTokens} Token`} · 供应商预估${run.usage.costMicros == null ? '待核实' : `¥${(run.usage.costMicros / 1000000).toFixed(6)}`}` : `本人Codex用量：${run.usage.totalTokens == null ? '未知' : `${run.usage.totalTokens} Token`} · 账户费用：未知`}</p>
        <div className="fmv-agent-fields"><button data-testid="agent-stop" disabled={busy || ['completed', 'failed', 'stopped'].includes(run.status)} onClick={() => { void control('stop'); }}><Square size={13} />停止后续操作</button><button data-testid="agent-resume" disabled={busy || !['conflict', 'waiting_connection', 'waiting_confirmation', 'stopped'].includes(run.status)} onClick={() => { void control('resume'); }}>按当前版本继续</button></div>
      </>}</section>}
      <details className="fmv-agent-memory"><summary><ChevronDown size={13} />项目记忆与来源</summary><button data-testid="agent-memory-refresh" disabled={busy} onClick={() => { void readMemories(); }}>读取设定与历史</button><form onSubmit={(event) => { void saveMemory(event); }}><label>保存类型<select value={memoryKind} onChange={(event) => setMemoryKind(event.target.value)}><option value="setting">项目设定</option><option value="decision">本人决定</option><option value="story_event">当前剧情事件</option></select></label><label>内容<textarea data-testid="agent-memory-content" value={memoryDraft} rows={3} required maxLength={10000} onChange={(event) => setMemoryDraft(event.target.value)} /></label><button data-testid="agent-memory-save" disabled={busy || disabled || !memoryDraft.trim()}>保存到项目记忆</button></form><ul>{memories.filter((item) => !item.sceneId || item.sceneId === sceneId).map((item) => <li key={item.id}><small>{memoryKinds[item.kind] || item.kind}{item.branchId ? ' · 限定剧情分支' : ''}{item.valid === false ? ' · 来源已更新' : ''}</small><p>{item.content}</p>{item.sources.map((source, index) => <button key={`${source.kind}:${source.id}:${index}`} disabled={busy} onClick={() => { void perform(async () => { setSourcePreview(await readMemorySource(projectId, source)); if (source.kind === 'canvas_node') onFocusSource(source.id); }); }}>来源 · 版本{source.revision}</button>)}</li>)}</ul>{sourcePreview && <div className="fmv-agent-source" data-testid="agent-source"><p>来源原文 · 版本{sourcePreview.source.revision}{sourcePreview.source.current ? '' : ' · 当前内容已更新'}</p><pre>{completionText(sourcePreview.content) || JSON.stringify(sourcePreview.content, null, 2)}</pre><button onClick={() => setSourcePreview(null)}>关闭来源</button></div>}</details>
      <details className="fmv-agent-connections" onToggle={(event) => { if (event.currentTarget.open) void reload(); }}>
        <summary><Link2 size={13} />本人Codex连接</summary>
        <p className="fmv-agent-muted">授权当前项目及下列工具，有效期1小时。双击产品目录的“连接Codex.command”运行连接程序，Codex登录凭据留在本机。</p>
        <p className="fmv-agent-muted">{role?.tools.map((tool) => agentToolTitles[tool] || tool).join('、')}</p>
        <button data-testid="agent-pair" disabled={busy || !role?.tools.length} onClick={() => { void perform(async () => { const result = await createCodexPairing(projectId, role?.tools || []); setPairing(result); }); }}>创建配对</button>
        <button type="button" disabled={busy} onClick={() => { void reload(); }}>刷新连接</button>
        {pairing && <div className="fmv-agent-pairing"><p>在本机连接程序填写下列配对信息：</p><label>配对标识<input readOnly value={pairing.pairingId} /></label><label>一次性配对码<input data-testid="agent-pairing-code" readOnly value={pairing.pairingCode} /></label><p>到期：{new Date(pairing.expiresAt).toLocaleTimeString()}</p><button onClick={() => setPairing(null)}>隐藏配对信息</button></div>}
        <ul>{connections.map((item) => <li key={item.id}><span>{agentStatuses[item.status] || item.status}</span><small> · {item.clientVersion || '版本待核对'}</small>{!['revoked', 'expired'].includes(item.status) && <button data-testid="agent-revoke" disabled={busy} onClick={() => { void perform(async () => { await revokeCodexConnection(projectId, item.id); await reload(); setNotice('连接授权已撤销。'); }); }}>撤销授权</button>}</li>)}</ul>
      </details>
    </div>
  </article>;
}

export default memo(AgentCanvasNode);
