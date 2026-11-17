'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowUp, Bot, Plus, Square, X } from 'lucide-react';
import { z } from 'zod';

import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogTitle } from '@/app/_components/ui/alert-dialog';
import { agentErrorMessage, agentStatuses, controlAgentRun, retryUnknownAgentRun } from '@/app/_utils/agentApi';
import { assetContentUrl, confirmGeneration, getCloudProject, getGenerations, operationId, resumeGeneration, type GenerationView } from '@/app/_utils/cloudApi';
import { createMaterialConversation, getMaterialConversation, listMaterialConversations, sendMaterialMessage } from '@/app/_utils/materialApi';
import { syncProjectSession } from '@/app/_utils/syncProjectSession';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { taskLabels } from '@/app/_features/creation-canvas/GenerationTaskCard';
import { materialRoles, useMaterialWorkspace } from './store';
import MaterialReview from './MaterialReview';
import './materials.css';

const busyStatuses = ['queued', 'running', 'stopping', 'waiting_confirmation', 'conflict', 'submission_unknown'];
const receiptsSchema = z.array(z.object({ tool: z.string(), result: z.record(z.string(), z.unknown()) }).passthrough());
const errorSchema = z.object({ code: z.string(), message: z.string() });
const boundarySchema = z.object({ frames: z.array(z.object({ type: z.enum(['first_frame', 'last_frame']), assetId: z.string() })) });

function materialError(value: unknown, canResume?: boolean) {
  const parsed = errorSchema.safeParse(value);
  if (parsed.success && (parsed.data.code.startsWith('AGENT_NATIVE_') || parsed.data.code.startsWith('AGENT_JSON_'))) {
    return canResume ? '本次生成已暂停，可以继续当前任务。' : '本次生成未完成，已保留产物和任务记录。';
  }
  return agentErrorMessage(value);
}

export default function MaterialChat({ onClose }: { onClose: () => void }) {
  const state = useMaterialWorkspace();
  const [busy, setBusy] = useState(false);
  const [confirmUnknown, setConfirmUnknown] = useState<string | null>(null);
  const [tasks, setTasks] = useState<GenerationView[]>([]);
  const [conversations, setConversations] = useState<{ id: string; title: string }[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const operationRef = useRef<{ content: string; references: string; revision: number; id: string } | null>(null);
  const retryRef = useRef<{ callId: string; operation: string } | null>(null);
  const value = state.conversation;
  const run = value?.messages.at(-1)?.run;
  const active = run && busyStatuses.includes(run.status);
  const switchingBlocked = busy || (!!active && run.status !== 'submission_unknown');
  const projectId = value?.projectId;
  const sceneId = value?.sceneId;
  const conversationId = value?.id;

  useEffect(() => {
    if (!projectId || !sceneId) return;
    let cancelled = false;
    void listMaterialConversations(projectId, sceneId).then((items) => { if (!cancelled) setConversations(items); }).catch((problem: Error) => { if (!cancelled) useMaterialWorkspace.setState({ error: problem.message }); });
    return () => { cancelled = true; };
  }, [projectId, sceneId, conversationId]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ block: 'nearest' }); }, [value?.messages.length, run?.status]);
  useEffect(() => {
    if (!value) return;
    let cancelled = false;
    const ids = new Set((value.messages.at(-1)?.run.steps || []).flatMap((step) => {
      const receipts = receiptsSchema.safeParse(step.toolResults);
      return receipts.success ? receipts.data.flatMap((receipt) => {
        const generation = z.object({ id: z.string() }).safeParse(receipt.result.generation);
        return receipt.tool === 'submit_generation' && generation.success ? [generation.data.id] : [];
      }) : [];
    }));
    if (!ids.size) { setTasks([]); return; }
    void getGenerations(value.projectId).then((response) => { if (!cancelled) setTasks(response.generations.filter((task) => ids.has(task.id))); }).catch((problem: Error) => { if (!cancelled) useMaterialWorkspace.setState({ error: problem.message }); });
    return () => { cancelled = true; };
  }, [value]);

  async function perform(action: () => Promise<void>) {
    setBusy(true); useMaterialWorkspace.setState({ error: '' });
    try { await action(); }
    catch (problem) { useMaterialWorkspace.setState({ error: problem instanceof Error ? problem.message : '操作失败' }); }
    finally { setBusy(false); }
  }
  async function send() {
    if (!value || !state.draft.trim() || busy || active) return;
    await perform(async () => {
      await state.savePending();
      await syncProjectSession(value.projectId);
      await useProjectSessionStore.getState().flushPendingChanges();
      const remote = await getCloudProject(value.projectId);
      const content = state.draft.trim();
      const references = JSON.stringify(state.references);
      const previous = operationRef.current;
      const operation = previous?.content === content && previous.references === references && previous.revision === remote.revision ? previous : { content, references, revision: remote.revision, id: operationId() };
      operationRef.current = operation;
      const updated = await sendMaterialMessage(value, content, state.references, remote.revision, operation.id);
      useMaterialWorkspace.setState({ conversation: updated, draft: '', references: [] });
      operationRef.current = null;
    });
  }
  async function control(action: 'stop' | 'resume') {
    if (!value || !run) return;
    await perform(async () => {
      const remote = await getCloudProject(value.projectId);
      await controlAgentRun(value.projectId, run.id, action, remote.revision);
      await state.refresh();
      await syncProjectSession(value.projectId);
    });
  }
  async function confirm(task: GenerationView) {
    if (!value) return;
    await perform(async () => {
      await useProjectSessionStore.getState().flushPendingChanges();
      const remote = await getCloudProject(value.projectId);
      await confirmGeneration(task, remote.revision);
      await state.refresh();
    });
  }
  async function retryUnknown() {
    if (!value || !run?.retryableUnknownCallId || confirmUnknown !== run.retryableUnknownCallId) return;
    const callId = run.retryableUnknownCallId;
    await perform(async () => {
      await state.savePending();
      await useProjectSessionStore.getState().flushPendingChanges();
      const remote = await getCloudProject(value.projectId);
      const operation = retryRef.current?.callId === callId ? retryRef.current : { callId, operation: operationId() };
      retryRef.current = operation;
      await retryUnknownAgentRun(value.projectId, run.id, callId, remote.revision, operation.operation, Math.max(40, run.limits?.maxSteps || 40));
      setConfirmUnknown(null);
      await state.refresh();
      await syncProjectSession(value.projectId);
    });
  }

  return <section className="material-chat" data-testid="material-chat">
    <header className="material-chat-header" data-chat-drag-handle><Bot size={19} /><div><strong>问问 AI</strong><span>素材总监与制作团队</span></div><button aria-label="新建素材对话" disabled={!value || switchingBlocked} onClick={() => { if (value) void perform(async () => { const created = await createMaterialConversation(value.projectId, value.sceneId); useMaterialWorkspace.setState({ conversation: created, references: [] }); }); }}><Plus size={17} /></button><button aria-label="关闭素材对话" onClick={onClose}><X size={17} /></button></header>
    {value && <label className="material-conversation-selector"><span>对话记录</span><select aria-label="素材对话记录" value={value.id} disabled={switchingBlocked} onChange={(event) => { const id = event.target.value; void perform(async () => { const selected = await getMaterialConversation(value.projectId, value.sceneId, id); useMaterialWorkspace.setState({ conversation: selected, references: [] }); }); }}>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>}
    <div className="material-chat-history">
      {!value?.messages.length && <div className="material-chat-welcome"><span className="material-director-avatar">总监</span><h3>你想制作什么素材？</h3><p>告诉我故事想法、画面要求，或者选择已有产物提出修改。我会梳理需求，安排对应岗位接续制作。</p><p className="material-subtle">剧本 → 角色与场景 → 分镜 → 视频</p></div>}
      {value?.messages.map((message) => <div key={message.id} className="material-turn">
        <div className="material-user-message">{message.references.length > 0 && <div className="material-reference-list">{message.references.map((reference) => <span key={reference.nodeId}>引用：{reference.title} · 版本 {reference.revision}</span>)}</div>}{message.content}</div>
        <div className="material-director-message"><strong>总监</strong><p>{message.run.plan.length ? '已安排制作岗位，产物会保存到对应区域。' : agentStatuses[message.run.status] || message.run.status}</p>
          {message.run.plan.length > 0 && <ol className="material-handoffs">{message.run.steps.map((step, index) => {
            return <li key={step.id || index}><span>{materialRoles.find((role) => role.id === step.role)?.title || step.role}</span><strong>{step.title}</strong><span className="material-subtle">{agentStatuses[step.status || ''] || step.status}</span></li>;
          })}</ol>}
          {message.run.error != null && ['failed', 'conflict', 'submission_unknown'].includes(message.run.status) && <p role="alert" className="material-error">{materialError(message.run.error, message.run.canResume)}</p>}
        </div>
      </div>)}
      {tasks.map((task) => { const boundaries = boundarySchema.safeParse(task.input.boundaryFrames); return <article className="material-media-task" key={task.id} data-testid="material-media-task">
        <strong>视频师 · {taskLabels[task.status] || task.status}</strong>
        <p>{String(task.input.model)} · {task.input.resolution} · {task.input.duration} 秒 · {String(task.input.count)} 个</p>
        {boundaries.success && <div className="material-video-boundaries">{boundaries.data.frames.map((frame) => <figure key={frame.type}><img src={assetContentUrl(frame.assetId)} alt={frame.type === 'first_frame' ? '视频首帧' : '视频尾帧'} /><figcaption>{frame.type === 'first_frame' ? '首帧' : '尾帧'}</figcaption></figure>)}</div>}
        {task.status === 'awaiting_confirmation' && <><p>请核对当前分镜画面与视频参数。</p><button className="material-primary" disabled={busy || task.projectRevision !== value?.revision} onClick={() => { void confirm(task); }}>确认生成</button>{task.projectRevision !== value?.revision && <p>输入版本已经变化，请停止当前任务后，让总监按当前素材重新准备生成。</p>}</>}
        {task.assetId && task.status === 'ready' && <video controls playsInline preload="metadata" src={assetContentUrl(task.assetId)} />}
        {task.error && <p role="alert">{agentErrorMessage(task.error)}</p>}
        {['query_interrupted', 'submission_unknown'].includes(task.status) && task.remoteTaskId && <button disabled={busy} onClick={() => { void perform(async () => { await resumeGeneration(task.id); await state.refresh(); }); }}>继续查询已有任务</button>}
      </article>; })}
      {value?.pendingReview && <MaterialReview key={`${value.pendingReview.id}-${value.pendingReview.status}-${value.pendingReview.questions.map((item) => item.id).join(',')}`} value={value} />}
      {run && (active || run.canResume) && <div className="material-run-controls">
        {active && run.status !== 'stopping' && <button disabled={busy} onClick={() => { void control('stop'); }}><Square size={13} />停止后续操作</button>}
        {(run.canResume || ['conflict', 'waiting_confirmation'].includes(run.status)) && !value?.pendingReview && <button disabled={busy || (run.status === 'waiting_confirmation' && tasks.some((task) => task.status === 'awaiting_confirmation'))} onClick={() => { void control('resume'); }}>{run.canResume ? '继续当前任务' : '按当前版本继续'}</button>}
        {run.retryableUnknownCallId && <button disabled={busy} onClick={() => setConfirmUnknown(run.retryableUnknownCallId ?? null)}>重新尝试当前步骤</button>}
      </div>}
      <AlertDialog open={!!run?.retryableUnknownCallId && confirmUnknown === run.retryableUnknownCallId} onOpenChange={(open) => { if (!open && !busy) setConfirmUnknown(null); }}>
        <AlertDialogContent><AlertDialogTitle>确认重新尝试当前步骤</AlertDialogTitle><AlertDialogDescription>上次请求是否已经处理仍待核实。重新尝试可能产生重复费用；原请求记录及费用预留继续保留，任务费用上限保持不变，请求上限为{Math.max(40, run?.limits?.maxSteps || 40)}次。</AlertDialogDescription>
          <AlertDialogFooter><AlertDialogCancel disabled={busy}>取消</AlertDialogCancel><AlertDialogAction disabled={busy} onClick={(event) => { event.preventDefault(); void retryUnknown(); }}>确认重新尝试</AlertDialogAction></AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <div ref={bottomRef} />
    </div>
    {state.error && <p role="alert" className="material-error">{state.error}</p>}
    <form className="material-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
      {state.references.length > 0 && <div className="material-reference-list">{state.references.map((reference) => <span key={reference.nodeId}>{reference.title} · 版本 {reference.revision}<button type="button" aria-label={`移除引用 ${reference.title}`} onClick={() => state.remove(reference.nodeId)}><X size={12} /></button></span>)}</div>}
      <textarea aria-label="素材需求" value={state.draft} onChange={(event) => useMaterialWorkspace.setState({ draft: event.target.value })} placeholder="描述素材需求，或选择画布产物提出修改…" disabled={!value || state.loading} rows={4} onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void send(); } }} />
      <footer><span>{active ? '当前任务处理中，可以先整理下一条需求' : '⌘ / Ctrl + Enter 发送'}</span><button aria-label="发送素材需求" type="submit" className="material-primary" disabled={busy || !!active || !state.draft.trim() || !value}><ArrowUp size={18} /></button></footer>
    </form>
  </section>;
}
