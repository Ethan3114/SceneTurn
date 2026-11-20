'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowUp, Bot, Plus, Square, X } from 'lucide-react';
import { z } from 'zod';

import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogTitle } from '@/app/_components/ui/alert-dialog';
import { Input } from '@/app/_components/ui/input';
import { agentErrorMessage, agentStatuses, controlAgentRun, extendAgentRunLimit, retryUnknownAgentRun } from '@/app/_utils/agentApi';
import { assetContentUrl, confirmGeneration, getCloudProject, getGenerations, operationId, quoteGenerationRetry, resumeGeneration, type GenerationView } from '@/app/_utils/cloudApi';
import { createMaterialConversation, getMaterialConversation, getProductionMessage, listMaterialConversations, sendMaterialMessage, type MaterialConversation, type MaterialMessageIntent } from '@/app/_utils/materialApi';
import { syncProjectSession } from '@/app/_utils/syncProjectSession';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { taskLabels } from '@/app/_features/creation-canvas/GenerationTaskCard';
import { materialRoles, useMaterialWorkspace } from './store';
import MaterialReview from './MaterialReview';
import MaterialReferenceImages, { requireProjectReferenceImages } from './MaterialReferenceImages';
import './materials.css';

const busyStatuses = ['queued', 'running', 'stopping', 'waiting_confirmation', 'conflict', 'submission_unknown'];
const unresolvedVideoStatuses = new Set(['awaiting_confirmation', 'queued', 'submitting', 'submission_unknown', 'processing', 'downloading', 'query_interrupted']);
const maxRequestLimit = 60;
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

function isCurrentConversation(value: MaterialConversation) {
  const current = useMaterialWorkspace.getState();
  return !current.loading && current.scope?.projectId === value.projectId && current.scope.sceneId === value.sceneId && current.conversation?.id === value.id;
}

export default function MaterialChat({ onClose }: { onClose: () => void }) {
  const state = useMaterialWorkspace();
  const [busy, setBusy] = useState(false);
  const [confirmUnknown, setConfirmUnknown] = useState<string | null>(null);
  const [limitChange, setLimitChange] = useState<{ runId: string; value: string } | null>(null);
  const [tasks, setTasks] = useState<GenerationView[]>([]);
  const [conversations, setConversations] = useState<{ id: string; title: string }[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const operationRef = useRef<{ conversationId: string; content: string; references: string; intent: MaterialMessageIntent; revision: number; id: string } | null>(null);
  const retryRef = useRef<{ callId: string; operation: string } | null>(null);
  const extensionRef = useRef<{ runId: string; revision: number; maxSteps: number; operation: string } | null>(null);
  const value = state.conversation;
  const currentMessage = getProductionMessage(value);
  const run = currentMessage?.run;
  const currentRequestLimit = run?.limits?.maxSteps ?? maxRequestLimit;
  const canExtend = run?.canExtendLimit === true && currentRequestLimit < maxRequestLimit;
  const requestedLimit = Number(limitChange?.value);
  const validLimit = Number.isInteger(requestedLimit) && requestedLimit > currentRequestLimit && requestedLimit <= maxRequestLimit;
  const active = run && busyStatuses.includes(run.status);
  const replacedVideoIds = new Set(tasks.flatMap((task) => task.retryOf && task.status === 'ready' ? [task.retryOf] : []));
  const waitingForVideo = tasks.some((task) => unresolvedVideoStatuses.has(task.status) && !replacedVideoIds.has(task.id));
  const canStartProduction = value?.canStartProduction ?? (!active && !waitingForVideo);
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

  useEffect(() => { bottomRef.current?.scrollIntoView({ block: 'nearest' }); }, [value?.messages.length, run?.status, value?.pendingReview?.id]);
  useEffect(() => {
    if (!value) return;
    let cancelled = false;
    const ids = new Set((currentMessage?.run.steps || []).flatMap((step) => {
      const receipts = receiptsSchema.safeParse(step.toolResults);
      return receipts.success ? receipts.data.flatMap((receipt) => {
        const generation = z.object({ id: z.string() }).safeParse(receipt.result.generation);
        return receipt.tool === 'submit_generation' && generation.success ? [generation.data.id] : [];
      }) : [];
    }));
    if (!ids.size) { setTasks([]); return; }
    void getGenerations(value.projectId).then((response) => {
      if (!cancelled) setTasks(response.generations.filter((task) => ids.has(task.id) || (task.retryOf && ids.has(task.retryOf))));
    }).catch((problem: Error) => { if (!cancelled) useMaterialWorkspace.setState({ error: problem.message }); });
    return () => { cancelled = true; };
  }, [value, currentMessage]);

  async function perform(action: () => Promise<void>) {
    if (useMaterialWorkspace.getState().loading) return;
    setBusy(true); useMaterialWorkspace.setState({ error: '' });
    try { await action(); }
    catch (problem) { useMaterialWorkspace.setState({ error: problem instanceof Error ? problem.message : '操作失败' }); }
    finally { setBusy(false); }
  }
  async function send(intent: MaterialMessageIntent = currentMessage ? 'followup' : 'start') {
    if (!value || !state.draft.trim() || busy || (intent === 'start' && !canStartProduction) || !isCurrentConversation(value)) return;
    const { draft, references, referenceAssetIds } = useMaterialWorkspace.getState();
    await perform(async () => {
      requireProjectReferenceImages(value.projectId, referenceAssetIds);
      let revision = value.revision;
      if (intent === 'start') {
        await state.savePending();
        await syncProjectSession(value.projectId);
        await useProjectSessionStore.getState().flushPendingChanges();
        revision = (await getCloudProject(value.projectId)).revision;
      }
      if (!isCurrentConversation(value)) return;
      const referenceKey = JSON.stringify({ nodes: references, assets: referenceAssetIds });
      const previous = operationRef.current;
      const operation = previous?.conversationId === value.id && previous.content === draft && previous.references === referenceKey && previous.intent === intent && previous.revision === revision
        ? previous
        : { conversationId: value.id, content: draft, references: referenceKey, intent, revision, id: operationId() };
      operationRef.current = operation;
      const updated = await sendMaterialMessage(value, draft, references, revision, operation.id, referenceAssetIds, intent);
      if (isCurrentConversation(value)) useMaterialWorkspace.setState((current) => ({
        conversation: updated,
        draft: current.draft === draft ? '' : current.draft,
        references: current.references === references ? [] : current.references,
        referenceAssetIds: current.referenceAssetIds === referenceAssetIds ? [] : current.referenceAssetIds,
      }));
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
  async function prepareVideoRetry(task: GenerationView) {
    if (!value || !isCurrentConversation(value)) return;
    await perform(async () => {
      const { generation } = await quoteGenerationRetry(task.id, value.revision);
      if (!isCurrentConversation(value)) return;
      setTasks((current) => current.some((item) => item.id === generation.id) ? current.map((item) => item.id === generation.id ? generation : item) : [...current, generation]);
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
  async function extendLimit() {
    if (!value || !run || !canExtend || !validLimit || limitChange?.runId !== run.id || !isCurrentConversation(value)) return;
    await perform(async () => {
      const remote = await getCloudProject(value.projectId);
      if (!isCurrentConversation(value) || getProductionMessage(useMaterialWorkspace.getState().conversation)?.run.id !== run.id) return;
      const previous = extensionRef.current;
      const request = previous?.runId === run.id && previous.revision === remote.revision && previous.maxSteps === requestedLimit
        ? previous
        : { runId: run.id, revision: remote.revision, maxSteps: requestedLimit, operation: operationId() };
      extensionRef.current = request;
      await extendAgentRunLimit(value.projectId, run.id, request.revision, request.operation, request.maxSteps);
      setLimitChange(null);
      await state.refresh();
      await syncProjectSession(value.projectId);
    });
  }

  return <section className="material-chat" data-testid="material-chat">
    <header className="material-chat-header" data-chat-drag-handle><Bot size={19} /><div><strong>问问 AI</strong><span>素材总编与制作团队</span></div><button aria-label="新建素材对话" disabled={!value || switchingBlocked || state.loading} onClick={() => { if (value && isCurrentConversation(value)) void perform(async () => { const created = await createMaterialConversation(value.projectId, value.sceneId); if (isCurrentConversation(value)) useMaterialWorkspace.setState({ conversation: created, references: [], referenceAssetIds: [] }); }); }}><Plus size={17} /></button><button aria-label="关闭素材对话" onClick={onClose}><X size={17} /></button></header>
    {value && <label className="material-conversation-selector"><span>对话记录</span><select aria-label="素材对话记录" value={value.id} disabled={switchingBlocked || state.loading} onChange={(event) => { const id = event.target.value; if (!isCurrentConversation(value)) return; void perform(async () => { const selected = await getMaterialConversation(value.projectId, value.sceneId, id); if (isCurrentConversation(value)) useMaterialWorkspace.setState({ conversation: selected, references: [], referenceAssetIds: [] }); }); }}>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>}
    <div className="material-chat-history">
      {!value?.messages.length && <div className="material-chat-welcome"><span className="material-director-avatar">总编</span><h3>你想制作什么素材？</h3><p>告诉我故事想法、画面要求，或者选择已有产物提出修改。我会梳理需求，安排对应岗位接续制作。</p><p className="material-subtle">剧本 → 角色与场景 → 分镜 → 视频</p></div>}
      {value?.messages.map((message) => {
        const isCurrent = message.id === currentMessage?.id;
        const followup = message.kind === 'followup';
        const referenceAssets = message.referenceAssets ?? (isCurrent ? value.selectedReferenceAssets : []);
        const brief = message.directorBrief ?? (isCurrent ? value.directorBrief : undefined);
        const status = brief?.statusText || (isCurrent && value.pendingReview ? '' : agentStatuses[message.run.status] || message.run.status);
        return <div key={message.id} className="material-turn">
          <div className={message.source === 'automation' ? 'material-automation-message' : 'material-user-message'}>
            {message.references.length > 0 && <div className="material-reference-list">{message.references.map((reference) => <span key={reference.nodeId}>引用：{reference.title} · 版本 {reference.revision}</span>)}</div>}
            {referenceAssets.length > 0 && <div className="material-reference-list">{referenceAssets.map((asset) => <span key={asset.assetId}>参考图片：{asset.name}</span>)}</div>}
            {message.content}
          </div>
          <div className="material-director-message">
            <strong>总编</strong>
            {followup ? <p className="material-brief-copy" data-testid="material-followup-reply">{message.reply}</p> : <>
            {brief?.objective && <p className="material-brief-copy">{brief.objective}</p>}
            {!!brief?.confirmedRequirements.length && <div className="material-confirmed-requirements">
              <span className="material-subtle">已确认要求</span>
              <ul>{brief.confirmedRequirements.map((requirement, index) => <li key={index}>{requirement}</li>)}</ul>
            </div>}
            {status && <p className={brief?.objective ? 'material-subtle material-brief-copy' : 'material-brief-copy'}>{status}</p>}
            {message.run.steps.length > 0 && <ol className="material-handoffs">{message.run.steps.map((step, index) => (
              <li key={step.id || index}><span>{materialRoles.find((role) => role.id === step.role)?.title || step.role}</span><strong>{step.title}</strong><span className="material-subtle">{agentStatuses[step.status || ''] || step.status}</span></li>
            ))}</ol>}
            {message.run.error != null && ['failed', 'conflict', 'submission_unknown'].includes(message.run.status) && <p role="alert" className="material-error">{materialError(message.run.error, message.run.canResume)}</p>}
            </>}
          </div>
        </div>;
      })}
      {tasks.map((task) => { const boundaries = boundarySchema.safeParse(task.input.boundaryFrames); return <article className="material-media-task" key={task.id} data-testid="material-media-task">
        <strong>视频师 · {task.retryOf ? '再次生成' : '原任务'} · {taskLabels[task.status] || task.status}</strong>
        <p>{String(task.input.model)} · {task.input.resolution} · {task.input.duration} 秒 · {String(task.input.count)} 个</p>
        {boundaries.success && <div className="material-video-boundaries">{boundaries.data.frames.map((frame) => <figure key={frame.type}><img src={assetContentUrl(frame.assetId)} alt={frame.type === 'first_frame' ? '视频首帧' : '视频尾帧'} /><figcaption>{frame.type === 'first_frame' ? '首帧' : '尾帧'}</figcaption></figure>)}</div>}
        {task.status === 'awaiting_confirmation' && <><p>{task.retryOf ? '再次提交可能产生重复费用。原请求和原费用预留继续保留。' : '请核对当前分镜画面与视频参数。'}</p><button className="material-primary" disabled={busy || task.projectRevision !== value?.revision} onClick={() => { void confirm(task); }}>{task.retryOf ? '确认再次生成' : '确认生成'}</button>{task.projectRevision !== value?.revision && <p>输入版本已经变化，请停止当前任务后，让总编按当前素材重新准备生成。</p>}</>}
        {task.assetId && task.status === 'ready' && <video controls playsInline preload="metadata" src={assetContentUrl(task.assetId)} />}
        {task.error && <p role="alert">{agentErrorMessage(task.error)}</p>}
        {['query_interrupted', 'submission_unknown'].includes(task.status) && (task.canResume ?? Boolean(task.remoteTaskId)) && <button disabled={busy} onClick={() => { void perform(async () => { await resumeGeneration(task.id); await state.refresh(); }); }}>继续查询已有任务</button>}
        {task.canRetry && !tasks.some((item) => item.retryOf === task.id) && <button disabled={busy} onClick={() => { void prepareVideoRetry(task); }}>核对首尾帧并准备再次生成</button>}
      </article>; })}
      {value?.pendingReview && <MaterialReview key={`${value.pendingReview.id}-${value.pendingReview.kind}-${value.pendingReview.status}-${value.pendingReview.questions.map((item) => item.id).join(',')}`} value={value} />}
      {run && (active || run.canResume || canExtend) && <div className="material-run-controls">
        {active && run.status !== 'stopping' && <button disabled={busy} onClick={() => { void control('stop'); }}><Square size={13} />停止后续操作</button>}
        {(run.canResume || ['conflict', 'waiting_confirmation'].includes(run.status)) && !value?.pendingReview && <button disabled={busy || waitingForVideo} onClick={() => { void control('resume'); }}>{run.canResume ? '继续当前任务' : '按当前版本继续'}</button>}
        {run.retryableUnknownCallId && <button disabled={busy} onClick={() => setConfirmUnknown(run.retryableUnknownCallId ?? null)}>重新尝试当前步骤</button>}
        {canExtend && <button disabled={busy} onClick={() => setLimitChange({ runId: run.id, value: String(Math.min(maxRequestLimit, currentRequestLimit + 8)) })}>提高模型请求上限</button>}
      </div>}
      <AlertDialog open={canExtend && !!run && limitChange?.runId === run.id} onOpenChange={(open) => { if (!open && !busy) setLimitChange(null); }}>
        <AlertDialogContent>
          <AlertDialogTitle>提高模型请求上限</AlertDialogTitle>
          <AlertDialogDescription>当前上限为 {currentRequestLimit} 次。确认后，任务会按原费用上限继续。</AlertDialogDescription>
          <label className="grid gap-2 text-sm">新的模型请求上限
            <Input type="number" min={currentRequestLimit + 1} max={maxRequestLimit} step={1} value={limitChange?.value ?? ''} disabled={busy}
              onChange={(event) => { if (run) setLimitChange({ runId: run.id, value: event.target.value }); }} />
          </label>
          <AlertDialogFooter><AlertDialogCancel disabled={busy}>取消</AlertDialogCancel><AlertDialogAction disabled={busy || !validLimit} onClick={(event) => { event.preventDefault(); void extendLimit(); }}>确认并继续</AlertDialogAction></AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
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
      {value && <MaterialReferenceImages projectId={value.projectId} selectedIds={state.referenceAssetIds} onChange={(referenceAssetIds) => useMaterialWorkspace.setState({ referenceAssetIds })} disabled={busy || state.loading} />}
      <textarea aria-label="素材需求" value={state.draft} onChange={(event) => useMaterialWorkspace.setState({ draft: event.target.value })} placeholder={currentMessage ? '询问当前进度，或补充这次制作要求…' : '描述素材需求，或选择画布产物提出修改…'} disabled={!value || state.loading} rows={4} onKeyDown={(event) => { if (event.key === 'Enter' && !event.nativeEvent.isComposing && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void send(); } }} />
      <footer>
        <span>{currentMessage ? '消息接在当前任务上' : '⌘ / Ctrl + Enter 发送'}</span>
        <div className="material-actions">
          {currentMessage && canStartProduction && <button type="button" disabled={busy || state.loading || !state.draft.trim()} onClick={() => { void send('start'); }}>开始新一轮制作</button>}
          <button aria-label={currentMessage ? '发送素材消息' : '发送素材需求'} type="submit" className="material-primary" disabled={busy || state.loading || !state.draft.trim() || !value || (!currentMessage && !canStartProduction)}><ArrowUp size={18} /></button>
        </div>
      </footer>
    </form>
  </section>;
}
