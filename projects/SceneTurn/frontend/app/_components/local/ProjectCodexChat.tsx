'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';
import { Link2, Loader2, PanelTop, Plus, Send, Square, X } from 'lucide-react';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { useTaskPolling } from '@/app/_hooks/useTaskPolling';
import { useEditorStore } from '@/app/_store/useEditorStore';
import { agentErrorMessage, agentStatuses, controlAgentRun, createCodexPairing, getCodexConnections } from '@/app/_utils/agentApi';
import { handOffCodexPairing, openCodexLauncher } from '@/app/_utils/codexLauncher';
import { createConversation, getConversation, getConversationCapabilities, getConversations, sendConversationMessage, type ConversationSummary, type ProjectConversation } from '@/app/_utils/conversationApi';
import { syncProjectSession } from '@/app/_utils/syncProjectSession';
import InteractionDesignView from './InteractionDesignView';

type Props = { floating?: boolean; onToggleFloating?: () => void; onClose?: () => void };
type PairingStage = 'opening' | 'waiting' | 'launcher-unavailable' | 'manual';
const terminal = new Set(['completed', 'failed', 'stopped']);
const toolLabels: Record<string, string> = { read_story: '读取项目', apply_story_ops: '保存剧情', arrange_story: '调整剧情排布', edit_timeline: '剪辑媒体', move_timeline_clip: '移动片段', edit_interaction: '保存互动', place_interaction: '调整按钮位置', undo_project_edit: '撤销编辑', prepare_scene_media: '准备场景素材', validate_story: '检查保存结果' };

export default function ProjectCodexChat({ floating = false, onToggleFloating, onClose }: Props) {
  const params = useSearchParams();
  const pathname = usePathname();
  const projectId = params.get('id') || '';
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [current, setCurrent] = useState<ProjectConversation | null>(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [pairing, setPairing] = useState<Awaited<ReturnType<typeof createCodexPairing>> | null>(null);
  const [pairingStage, setPairingStage] = useState<PairingStage | null>(null);
  const activePairing = pairing?.projectId === projectId ? pairing : null;
  const manualPairing = pairingStage === 'manual' || pairingStage === 'launcher-unavailable';
  const revisionSeen = useRef(0);
  const refreshSelection = useRef<(() => Promise<void>) | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const run = current?.run;
  const running = Boolean(run && !terminal.has(run.status));
  const pollingRun = run?.status === 'queued' || run?.status === 'running' || run?.status === 'stopping'
    || (run?.status === 'waiting_connection' && connected);
  const pollingPairing = Boolean(activePairing && !connected);

  const apply = useCallback(async (value: ProjectConversation) => {
    setCurrent(value);
    const revision = value.run?.currentRevision || value.run?.projectRevision || 0;
    if (revision && revision !== revisionSeen.current) {
      const conflicts = await syncProjectSession(projectId);
      if (conflicts.length) setNotice('保留了你的手动编辑；相同字段的Codex结果可在任务历史中核对。');
      revisionSeen.current = revision;
    }
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    void getConversations(projectId).then(async (values) => {
      if (!alive) return;
      setConversations(values);
      const value = values[0] ? await getConversation(projectId, values[0].id) : await createConversation(projectId);
      if (alive) { await apply(value); setConversations((items) => items.some((item) => item.id === value.id) ? items : [value, ...items]); }
    }).catch((problem: Error) => { if (alive) setError(problem.message); });
    return () => { alive = false; };
  }, [projectId, apply]);

  useEffect(() => {
    if (!projectId || !current?.id) return;
    let alive = true;
    const refresh = async () => {
      const [value, connections] = await Promise.all([getConversation(projectId, current.id), getCodexConnections(projectId)]);
      if (!alive) return;
      setConnected(connections.some((item) => item.status === 'online' && new Date(item.expiresAt).getTime() > Date.now()));
      await apply(value);
    };
    const refreshCurrent = () => refresh().catch((problem: Error) => { if (alive) setError(problem.message); });
    refreshSelection.current = refreshCurrent;
    const refreshFocused = () => { void refreshCurrent(); };
    void refreshCurrent();
    window.addEventListener('focus', refreshFocused);
    return () => {
      alive = false;
      refreshSelection.current = null;
      window.removeEventListener('focus', refreshFocused);
    };
  }, [projectId, current?.id, apply]);
  useTaskPolling(Boolean(projectId && current?.id && (pollingRun || pollingPairing)), 1500, () => refreshSelection.current?.());

  useEffect(() => {
    if (!pairing) return;
    const timer = window.setTimeout(() => {
      setPairing((currentPairing) => currentPairing?.pairingId === pairing.pairingId ? null : currentPairing);
    }, Math.max(0, new Date(pairing.expiresAt).getTime() - Date.now()));
    return () => window.clearTimeout(timer);
  }, [pairing]);

  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }); }, [current?.messages.length]);

  async function perform(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError('');
    await action().catch((problem: Error) => setError(problem.message)).finally(() => setBusy(false));
  }
  async function connect() {
    await perform(async () => {
      const openingLauncher = openCodexLauncher();
      setPairingStage(openingLauncher ? 'opening' : null);
      try {
        const capability = await getConversationCapabilities(projectId);
        const request = await createCodexPairing(projectId, capability.tools);
        setPairing(request);
        if (!openingLauncher) {
          setPairingStage('manual');
          return;
        }
        setPairingStage(await handOffCodexPairing(request) ? 'waiting' : 'launcher-unavailable');
      } finally {
        setPairingStage((stage) => stage === 'opening' ? null : stage);
      }
    });
  }
  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!current || !draft.trim() || running) return;
    await perform(async () => {
      const state = useProjectSessionStore.getState();
      const selected = state.nodes.filter((node) => node.selected);
      const selectedId = useEditorStore.getState().selectedNodeId;
      const scene = selected.find((node) => node.type === 'scene') || state.nodes.find((node) => node.id === selectedId && node.type === 'scene') || state.nodes.find((node) => node.type === 'scene');
      if (state.projectId !== projectId || !scene) throw new Error('请先在剧情编排中打开当前项目并创建场景。');
      await syncProjectSession(projectId);
      await useProjectSessionStore.getState().flushPendingChanges();
      const revision = useProjectSessionStore.getState().project?.metadata.serverRevision;
      if (!revision) throw new Error('项目版本尚未读取完成。');
      const value = await sendConversationMessage(projectId, current.id, draft.trim(), revision, { sceneId: scene.id, selectedNodeIds: selected.map((node) => node.id), surface: pathname.includes('/nodes') ? 'timeline' : 'editor' });
      await apply(value); setDraft('');
      setConversations((items) => [value, ...items.filter((item) => item.id !== value.id)]);
      void refreshSelection.current?.();
    });
  }
  async function stopOrResume(action: 'stop' | 'resume') {
    if (!run || !current) return;
    await perform(async () => {
      if (action === 'resume') { await syncProjectSession(projectId); await useProjectSessionStore.getState().flushPendingChanges(); }
      const revision = useProjectSessionStore.getState().project?.metadata.serverRevision || run.currentRevision || run.projectRevision;
      await controlAgentRun(projectId, run.id, action, revision);
      await refreshSelection.current?.();
    });
  }

  if (!projectId && typeof window !== 'undefined' && window.sceneturn) return <InteractionDesignView variant="panel" floating={floating} onToggleFloating={onToggleFloating} onClose={onClose} />;
  return <section className="flex h-full min-h-0 flex-col bg-[#111113] text-white" data-testid="project-codex-chat">
    <header data-chat-drag-handle className="flex h-11 shrink-0 items-center justify-between border-b border-white/[0.08] px-3">
      <span className="text-sm font-semibold">问问 AI · 本人 Codex</span>
      <div className="flex gap-2"><button title="新对话" data-testid="chat-new" disabled={busy || !projectId} onClick={() => { void perform(async () => { const value = await createConversation(projectId); setCurrent(value); setConversations((items) => [value, ...items]); }); }}><Plus size={16} /></button><button title="切换浮动窗口" onClick={onToggleFloating}><PanelTop size={16} /></button><button title="关闭对话" onClick={onClose}><X size={17} /></button></div>
    </header>
    {!projectId ? <p className="p-5 text-sm text-white/60">打开项目后，可以与本人Codex讨论剧情并操作画布。</p> : <>
      <div className="space-y-2 border-b border-white/10 p-3 text-xs">
        <div className="flex justify-between"><span data-testid="chat-connection">{connected ? '本人Codex已连接' : '等待本机连接'}</span><button type="button" data-testid="chat-connect" disabled={busy} onClick={() => { void connect(); }}><Link2 size={13} className="mr-1 inline" />连接本人Codex</button></div>
        <select aria-label="项目对话记录" data-testid="chat-conversation-select" className="w-full rounded border border-white/15 bg-[#202020] p-2" value={current?.id || ''} onChange={(event) => { void perform(async () => { await apply(await getConversation(projectId, event.target.value)); }); }}>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select>
        {!connected && pairingStage === 'opening' && <p role="status" className="text-white/70">正在打开连接程序。浏览器提示访问本机设备时，请选择允许。</p>}
        {!connected && activePairing && pairingStage !== 'opening' && <div className="space-y-2 rounded border border-white/15 p-3">
          {pairingStage === 'waiting' && <p role="status">配对请求已发送，正在等待连接程序上线。</p>}
          {pairingStage === 'launcher-unavailable' && <p role="status">首次使用需要先安装 SceneTurn 连接程序。若已安装，请允许浏览器访问本机设备，然后再次点击「连接本人Codex」。</p>}
          <details open={manualPairing || undefined} className="space-y-2">
            <summary className="cursor-pointer">手动配对</summary>
            <p>双击产品目录的「连接Codex.command」，填写下列信息。</p>
            <label className="block">工作台服务地址<input readOnly className="w-full bg-black/30 p-2" value={`${window.location.origin}/api/v1`} /></label>
            <label className="block">配对标识<input readOnly className="w-full bg-black/30 p-2" value={activePairing.pairingId} /></label>
            <label className="block">一次性配对码<input readOnly className="w-full bg-black/30 p-2" value={activePairing.pairingCode} /></label>
            <button type="button" onClick={() => setPairing(null)}>隐藏配对信息</button>
          </details>
        </div>}
      </div>
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4" data-testid="chat-messages">
        {!current?.messages.length && <p className="text-sm leading-7 text-white/50">可以讨论整体剧情、排列场景、剪辑已有媒体、添加按钮或QTE。选中画布对象后发送要求，Codex会读取当前项目并按Skill执行。</p>}
        {current?.messages.map((item) => <article key={item.id} data-testid={`chat-message-${item.role}`} className={`whitespace-pre-wrap break-words rounded-xl p-3 text-sm leading-7 ${item.role === 'user' ? 'ml-5 bg-white/[0.09]' : 'mr-3 bg-white/[0.035]'}`}><small className="mb-1 block text-white/40">{item.role === 'user' ? '你' : '本人Codex'}</small>{item.content}</article>)}
        {run && <div className="space-y-2 rounded border border-white/10 p-3 text-xs text-white/65"><p data-testid="chat-run-status">{agentStatuses[run.status] || run.status}</p>{run.steps.flatMap((step) => Array.isArray(step.toolResults) ? step.toolResults : []).map((receipt: { operationId?: string; tool?: string }, index: number) => <p key={receipt.operationId || index}>{toolLabels[receipt.tool || ''] || receipt.tool}</p>)}{run.status === 'completed' && <p>{run.projectRevision > (run.initialRevision ?? run.projectRevision) ? '修改已保存到当前项目' : '本轮未修改项目'}</p>}{agentErrorMessage(run.error) && <p role="alert">{agentErrorMessage(run.error)}</p>}{run.usage.totalTokens != null && <p>本人Codex用量：{run.usage.totalTokens} Token</p>}</div>}
      </div>
      {error && <p role="alert" className="px-4 py-2 text-xs text-red-300">{error}</p>}{notice && <p role="status" className="px-4 py-2 text-xs text-amber-200">{notice}</p>}
      <form onSubmit={(event) => { void send(event); }} className="space-y-2 border-t border-white/10 p-3"><textarea data-testid="chat-input" aria-label="给本人Codex的消息" value={draft} onChange={(event) => setDraft(event.target.value)} maxLength={12000} rows={3} className="w-full resize-none rounded-lg border border-white/15 bg-black/20 p-3 text-sm" placeholder="描述剧情或编辑要求，支持连续追问" /><div className="flex justify-end gap-2">{running && <button type="button" data-testid="chat-stop" className="rounded border border-white/15 px-3 py-2 text-xs" disabled={busy} onClick={() => { void stopOrResume('stop'); }}><Square size={12} className="mr-1 inline" />停止</button>}{run && ['stopped', 'conflict', 'waiting_connection'].includes(run.status) && <button type="button" data-testid="chat-resume" disabled={busy} onClick={() => { void stopOrResume('resume'); }} className="rounded border border-white/15 px-3 py-2 text-xs">按当前项目继续</button>}<button data-testid="chat-send" className="rounded-lg bg-white/15 px-4 py-2 text-sm disabled:opacity-40" disabled={busy || running || !draft.trim() || !current}>{busy ? <Loader2 size={15} className="animate-spin" /> : <><Send size={14} className="mr-2 inline" />发送</>}</button></div></form>
    </>}
  </section>;
}
