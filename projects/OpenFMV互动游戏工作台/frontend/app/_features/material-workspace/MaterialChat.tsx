'use client';

import { useEffect, useRef, useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { renameMaterialConversation } from '@/app/_utils/materialApi';

import { Check, Clock3, Film, MessageSquare, Plus, SlidersHorizontal, Square, X } from 'lucide-react';
import { z } from 'zod';

import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogTitle } from '@/app/_components/ui/alert-dialog';
import { Input } from '@/app/_components/ui/input';
import { agentErrorMessage, agentStatuses, controlAgentRun, extendAgentRunLimit, retryUnknownAgentRun } from '@/app/_utils/agentApi';
import { assetContentUrl, confirmGeneration, getCloudProject, operationId, quoteGenerationRetry, resumeGeneration, type GenerationView } from '@/app/_utils/cloudApi';
import { createMaterialConversation, getMaterialConversation, getProductionMessage, listMaterialConversations, sendMaterialMessage, type MaterialConversation, type MaterialMessageIntent } from '@/app/_utils/materialApi';
import { getProviderCatalog, modelKinds, type ModelDefaults, type ProviderCatalog } from '@/app/_utils/providerApi';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import { syncProjectSession } from '@/app/_utils/syncProjectSession';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { taskLabels } from '@/app/_features/creation-canvas/GenerationTaskCard';
import { materialRoles, useMaterialWorkspace } from './store';
import MaterialReview from './MaterialReview';
import MaterialReferenceImages, { requireProjectReferenceImages } from './MaterialReferenceImages';
import ModelPickerDialog, { useModelDisplay } from './ModelPickerDialog';
import './materials.css';
import './material-chat.css';

const busyStatuses = ['queued', 'running', 'stopping', 'waiting_confirmation', 'conflict', 'submission_unknown'];
const emptyVideoTasks: GenerationView[] = [];
const unresolvedVideoStatuses = new Set(['awaiting_confirmation', 'queued', 'submitting', 'submission_unknown', 'processing', 'downloading', 'query_interrupted']);
const maxRequestLimit = 60;
const errorSchema = z.object({ code: z.string(), message: z.string() });
const boundarySchema = z.object({ frames: z.array(z.object({ type: z.enum(['first_frame', 'last_frame']), assetId: z.string() })) });
const shotSummarySchema = z.object({ title: z.string(), action: z.string(), camera: z.string(), sound: z.string() });
const statusTones: Record<string, string> = { completed: 'success', ready: 'success', waiting_confirmation: 'warning', submission_unknown: 'warning' };

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

function updateVideoResults(runId: string, task: GenerationView) {
  const current = useMaterialWorkspace.getState();
  if (current.loading || getProductionMessage(current.conversation)?.run.id !== runId) return;
  const previous = current.videoResults?.runId === runId ? current.videoResults.tasks : emptyVideoTasks;
  const tasks = previous.some((item) => item.id === task.id)
    ? previous.map((item) => item.id === task.id ? task : item)
    : [...previous, task];
  useMaterialWorkspace.setState({ videoResults: { runId, tasks } });
}

export default function MaterialChat({ onClose }: { onClose: () => void }) {
  const locale = useLocale();
  const t = useTranslations('materialModels');
  const displayModel = useModelDisplay();
  const state = useMaterialWorkspace();
  const [busy, setBusy] = useState(false);
  const [confirmUnknown, setConfirmUnknown] = useState<string | null>(null);
  const [limitChange, setLimitChange] = useState<{ runId: string; value: string } | null>(null);
  const [conversations, setConversations] = useState<{ id: string; title: string }[]>([]);
  const [historyQuery, setHistoryQuery] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [modelCatalog, setModelCatalog] = useState<{ projectId: string; request: number; data: ProviderCatalog | null; error?: string } | null>(null);
  const [catalogRequest, setCatalogRequest] = useState(0);
  const [modelDialogConversationId, setModelDialogConversationId] = useState<string | null>(null);
  const historyRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const handledVideoReview = useRef<{ conversationId: string; request: number } | null>(null);
  const operationRef = useRef<{ conversationId: string; content: string; references: string; intent: MaterialMessageIntent; revision: number; id: string } | null>(null);
  const retryRef = useRef<{ callId: string; operation: string } | null>(null);
  const extensionRef = useRef<{ runId: string; revision: number; maxSteps: number; operation: string } | null>(null);
  const value = state.conversation;
  const scopeSceneId = state.scope?.sceneId;
  const sceneTitle = useProjectSessionStore((session) => session.nodes.find((node) => node.id === scopeSceneId)?.data.title);
  const currentMessage = getProductionMessage(value);
  const run = currentMessage?.run;
  const { videoResults, videoReviewTaskId, videoReviewRequest, openVideoReview } = state;
  const tasks = videoResults && videoResults.runId === run?.id ? videoResults.tasks : emptyVideoTasks;
  const selectedVideo = tasks.find((task) => task.id === videoReviewTaskId);
  const currentRequestLimit = run?.limits?.maxSteps ?? maxRequestLimit;
  const canExtend = run?.canExtendLimit === true && currentRequestLimit < maxRequestLimit;
  const requestedLimit = Number(limitChange?.value);
  const validLimit = Number.isInteger(requestedLimit) && requestedLimit > currentRequestLimit && requestedLimit <= maxRequestLimit;
  const active = run && busyStatuses.includes(run.status);
  const pendingVideo = tasks.find((task) => task.status === 'awaiting_confirmation');
  const replacedVideoIds = new Set(tasks.flatMap((task) => task.retryOf && task.status === 'ready' ? [task.retryOf] : []));
  const waitingForVideo = tasks.some((task) => unresolvedVideoStatuses.has(task.status) && !replacedVideoIds.has(task.id));
  const canStartProduction = value?.canStartProduction ?? (!active && !waitingForVideo);
  const switchingBlocked = busy || (!!active && run.status !== 'submission_unknown');
  const projectId = value?.projectId;
  const sceneId = value?.sceneId;
  const conversationId = value?.id;
  const catalog = modelCatalog?.projectId === projectId ? modelCatalog?.data : undefined;
  const catalogLoading = Boolean(projectId && (modelCatalog?.projectId !== projectId || modelCatalog.request !== catalogRequest));
  const catalogError = catalogLoading ? undefined : modelCatalog?.error;
  const selection = state.modelDraft?.conversationId === conversationId ? state.modelDraft?.selection : undefined;
  const choosingModels = Boolean(conversationId && modelDialogConversationId === conversationId);
  const currentModels = currentMessage ? value?.capabilities.modelSelection : undefined;

  function modelSummary(models: ModelDefaults) {
    return modelKinds.map((kind) => {
      const model = catalog?.models.find(({ id }) => id === models[kind]);
      return `${t(kind)} ${model ? displayModel(model) : models[kind]}`;
    }).join(' · ');
  }

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    void getProviderCatalog(projectId).then((data) => {
      if (!cancelled) setModelCatalog({ projectId, request: catalogRequest, data });
    }).catch(() => {
      if (!cancelled) setModelCatalog((current) => ({ projectId, request: catalogRequest, data: current?.projectId === projectId ? current.data : null, error: t('loadError') }));
    });
    return () => { cancelled = true; };
  }, [projectId, catalogRequest, t]);

  useEffect(() => {
    if (!choosingModels) return;
    const refresh = () => setCatalogRequest((current) => current + 1);
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [choosingModels]);

  useEffect(() => {
    if (!projectId || !sceneId) return;
    let cancelled = false;
    void listMaterialConversations(projectId, sceneId).then((items) => { if (!cancelled) setConversations(items); }).catch((problem: Error) => { if (!cancelled) useMaterialWorkspace.setState({ error: problem.message }); });
    return () => { cancelled = true; };
  }, [projectId, sceneId, conversationId]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ block: 'nearest' }); }, [value?.messages.length, run?.status, value?.pendingReview?.id]);
  useEffect(() => {
    const handled = handledVideoReview.current;
    if (!conversationId || !videoReviewRequest || (handled?.conversationId === conversationId && handled.request === videoReviewRequest)) return;
    const card = historyRef.current?.querySelector<HTMLElement>('[data-video-task-id]');
    if (!card || card.dataset.videoTaskId !== videoReviewTaskId) return;
    card.scrollIntoView({ block: 'nearest' });
    card.focus({ preventScroll: true });
    handledVideoReview.current = { conversationId, request: videoReviewRequest };
  }, [conversationId, tasks, videoReviewRequest, videoReviewTaskId]);

  async function perform(action: () => Promise<void>) {
    if (useMaterialWorkspace.getState().loading) return;
    setBusy(true); useMaterialWorkspace.setState({ error: '' });
    try { await action(); }
    catch (problem) { useMaterialWorkspace.setState({ error: problem instanceof Error ? problem.message : '操作失败' }); }
    finally { setBusy(false); }
  }
  async function openConversation(id?: string) {
    if (!value || switchingBlocked || !isCurrentConversation(value)) return;
    await perform(async () => {
      const selected = id
        ? await getMaterialConversation(value.projectId, value.sceneId, id)
        : await createMaterialConversation(value.projectId, value.sceneId);
      if (!isCurrentConversation(value)) return;
      state.setConversation(selected);
      useMaterialWorkspace.setState({ references: [], referenceAssetIds: [] });
    });
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
      const modelSelection = intent === 'start' && selection && Object.keys(selection).length ? selection : undefined;
      const referenceKey = JSON.stringify({ nodes: references, assets: referenceAssetIds, modelSelection });
      const previous = operationRef.current;
      const operation = previous?.conversationId === value.id && previous.content === draft && previous.references === referenceKey && previous.intent === intent && previous.revision === revision
        ? previous
        : { conversationId: value.id, content: draft, references: referenceKey, intent, revision, id: operationId() };
      operationRef.current = operation;
      const updated = await sendMaterialMessage(value, draft, references, revision, operation.id, referenceAssetIds, intent, modelSelection);
      if (isCurrentConversation(value)) useMaterialWorkspace.setState((current) => ({
        conversation: updated,
        draft: current.draft === draft ? '' : current.draft,
        references: current.references === references ? [] : current.references,
        referenceAssetIds: current.referenceAssetIds === referenceAssetIds ? [] : current.referenceAssetIds,
        modelDraft: intent === 'start' && current.modelDraft?.selection === selection ? null : current.modelDraft,
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
    if (!value || !run || !isCurrentConversation(value)) return;
    await perform(async () => {
      await useProjectSessionStore.getState().flushPendingChanges();
      const remote = await getCloudProject(value.projectId);
      if (!isCurrentConversation(value)) return;
      const { generation } = await confirmGeneration(task, remote.revision);
      updateVideoResults(run.id, generation);
      await state.refresh();
    });
  }
  async function prepareVideoRetry(task: GenerationView) {
    if (!value || !run || !isCurrentConversation(value)) return;
    await perform(async () => {
      const { generation } = await quoteGenerationRetry(task.id, value.revision);
      if (!isCurrentConversation(value)) return;
      updateVideoResults(run.id, generation);
      openVideoReview(generation.id);
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

  return <section className={`material-chat material-chat-dock${selectedVideo ? ' is-video-review' : ''}`} data-testid="material-chat" aria-label="素材制作对话">
    <header className="material-chat-header" data-chat-drag-handle>
      <MessageSquare size={19} aria-hidden="true" />
      <div className="material-chat-heading"><h2>问问 AI</h2><p>素材制作{sceneTitle ? ` · ${sceneTitle}` : ''}</p></div>
      <div className="material-chat-header-actions">
        <button type="button" aria-label="新建素材对话" title="新建素材对话" disabled={!value || switchingBlocked || state.loading} onClick={() => { void openConversation(); }}><Plus size={17} aria-hidden="true" /></button>
        <button type="button" aria-label="关闭素材对话" onClick={onClose}>关闭</button>
      </div>
    </header>
    {!selectedVideo && value && conversations.length > 1 && <label className="material-conversation-selector"><span>对话记录</span><select aria-label="素材对话记录" value={value.id} disabled={switchingBlocked || state.loading} onChange={(event) => { void openConversation(event.target.value); }}>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>}
    {!selectedVideo && value && <div className="material-conversation-tools"><button aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>管理对话</button>{historyOpen && <div>
      <input type="search" aria-label="搜索对话" placeholder="搜索对话" value={historyQuery} onChange={e => setHistoryQuery(e.target.value)}/>
      {conversations.filter(item => item.title.toLowerCase().includes(historyQuery.trim().toLowerCase())).map(item => <button disabled={busy || switchingBlocked} aria-current={item.id === value.id} key={item.id} onClick={() => { void openConversation(item.id); }}>{item.title}</button>)}
      <form key={value.id} onSubmit={event => { event.preventDefault(); const title = String(new FormData(event.currentTarget).get('title') || '').trim(); if (!title) return; void perform(async () => { const updated = await renameMaterialConversation(value,title); if (!isCurrentConversation(value)) return; state.setConversation(updated); setConversations(items => items.map(item => item.id === value.id ? { ...item, title: updated.title } : item)); }); }}><label>当前对话名称<input name="title" maxLength={160} required defaultValue={value.title}/></label><button disabled={busy}>保存名称</button></form>
    </div>}</div>}
    <div className="material-chat-history" ref={historyRef} aria-busy={state.loading}>
      {state.loading && <p className="material-chat-loading" role="status">正在读取素材制作对话…</p>}
      {!state.loading && value && !value.messages.length && <div className="material-director-message material-chat-welcome"><strong>总编</strong><h3>你想制作什么素材？</h3><p>描述故事、镜头或画面要求，也可以引用已生成的素材继续修改。</p></div>}
      {!selectedVideo && value?.messages.map((message) => {
        const isCurrent = message.id === currentMessage?.id;
        const followup = message.kind === 'followup';
        const referenceAssets = message.referenceAssets ?? (isCurrent ? value.selectedReferenceAssets : []);
        const brief = message.directorBrief ?? (isCurrent ? value.directorBrief : undefined);
        const status = brief?.statusText || (isCurrent && value.pendingReview ? '' : agentStatuses[message.run.status] || message.run.status);
        const tone = statusTones[message.run.status];
        return <div key={message.id} className="material-turn">
          <div className={message.source === 'automation' ? 'material-automation-message' : 'material-user-message'}>
            <strong>{message.source === 'automation' ? '制作记录' : '你'}</strong>
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
            {status && <p className="material-chat-status" data-tone={tone}>
              {tone === 'success' && <Check size={13} aria-hidden="true" />}
              {tone === 'warning' && <Clock3 size={13} aria-hidden="true" />}
              {status}
            </p>}
            {message.run.steps.length > 0 && <ol className="material-handoffs">{message.run.steps.map((step, index) => (
              <li key={step.id || index}><span>{materialRoles.find((role) => role.id === step.role)?.title || step.role}</span><strong>{step.title}</strong><span className="material-subtle">{agentStatuses[step.status || ''] || step.status}</span></li>
            ))}</ol>}
            {message.run.error != null && ['failed', 'conflict', 'submission_unknown'].includes(message.run.status) && <p role="alert" className="material-error">{materialError(message.run.error, message.run.canResume)}</p>}
            </>}
          </div>
          {isCurrent && !followup && pendingVideo && <button className="material-chat-video-review" type="button" onClick={() => openVideoReview(pendingVideo.id)}>
            <Film size={16} aria-hidden="true" />核对视频镜头
          </button>}
        </div>;
      })}
      {(selectedVideo ? [selectedVideo] : tasks).map((task) => {
        const boundaries = boundarySchema.safeParse(task.input.boundaryFrames);
        const shot = shotSummarySchema.safeParse(task.input.shotSummary).data;
        const awaiting = task.status === 'awaiting_confirmation';
        const status = taskLabels[task.status] || task.status;
        return <article className="material-media-task" key={task.id} data-testid="material-media-task" data-video-task-id={task.id} data-video-review={awaiting ? 'pending' : undefined} tabIndex={-1}>
        <strong>{selectedVideo ? <Clock3 size={16} aria-hidden="true" /> : <Film size={16} aria-hidden="true" />}{selectedVideo ? (awaiting ? '等待确认' : status) : `视频师 · ${task.retryOf ? '再次生成' : '原任务'} · ${status}`}</strong>
        {selectedVideo && <>
          {awaiting && <p>首尾帧和镜头规格已准备。核对{shot?.title ? `「${shot.title}」` : '当前镜头'}后可提交生成。</p>}
          <p className="material-subtle">镜头调整可继续在本轮对话发给总编。</p>
        </>}
        {!selectedVideo && <p>{String(task.input.model)} · {task.input.resolution} · {task.input.duration} 秒 · {String(task.input.count)} 个</p>}
        {!selectedVideo && boundaries.success && <div className="material-video-boundaries">{boundaries.data.frames.map((frame) => <figure key={frame.type}><img src={assetContentUrl(frame.assetId)} alt={frame.type === 'first_frame' ? '视频首帧' : '视频尾帧'} /><figcaption>{frame.type === 'first_frame' ? '首帧' : '尾帧'}</figcaption></figure>)}</div>}
        {!selectedVideo && shot && <details open={awaiting} className="material-execution">
          <summary>{shot.title}</summary>
          <p><strong>动作与互动</strong>　{shot.action}</p>
          <p><strong>运镜</strong>　{shot.camera}</p>
          <p><strong>声音</strong>　{shot.sound}</p>
        </details>}
        {awaiting && <>{(task.retryOf || !selectedVideo) && <p>{task.retryOf ? '再次提交可能产生重复费用。原请求和原费用预留继续保留。' : '请核对当前分镜画面与视频参数。'}</p>}<button className="material-primary" disabled={busy || task.projectRevision !== value?.revision} onClick={() => { void confirm(task); }}>{task.retryOf ? '确认再次生成' : '确认生成'}</button>{task.projectRevision !== value?.revision && <p>输入版本已经变化，请停止当前任务后，让总编按当前素材重新准备生成。</p>}</>}
        {task.assetId && task.status === 'ready' && <video controls playsInline preload="metadata" src={assetContentUrl(task.assetId)} />}
        {task.error && <p role="alert">{agentErrorMessage(task.error)}</p>}
        {['query_interrupted', 'submission_unknown'].includes(task.status) && (task.canResume ?? Boolean(task.remoteTaskId)) && <button disabled={busy} onClick={() => { void perform(async () => { await resumeGeneration(task.id); await state.refresh(); }); }}>继续查询已有任务</button>}
        {task.canRetry && !tasks.some((item) => item.retryOf === task.id) && <button disabled={busy} onClick={() => { void prepareVideoRetry(task); }}>核对首尾帧并准备再次生成</button>}
      </article>; })}
      {!selectedVideo && value?.pendingReview && <MaterialReview key={`${value.pendingReview.id}-${value.pendingReview.kind}-${value.pendingReview.status}-${value.pendingReview.questions.map((item) => item.id).join(',')}`} value={value} />}
      {!selectedVideo && run && (active || run.canResume || canExtend) && <div className="material-run-controls">
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
    <form className="material-composer" data-testid="material-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
      {(state.references.length > 0 || currentModels || (selection && catalog)) && <div className="material-composer-context">
        {state.references.length > 0 && <div className="material-reference-list">{state.references.map((reference) => <span key={reference.nodeId}>{reference.title} · 版本 {reference.revision}<button type="button" aria-label={`移除引用 ${reference.title}`} onClick={() => state.remove(reference.nodeId)}><X size={12} /></button></span>)}</div>}
        {currentModels && <p className="material-model-summary" data-testid="material-current-models">{t('currentModels')} · {modelSummary(currentModels)}</p>}
        {selection && catalog && <p className="material-model-summary" data-testid="material-next-models">{t('nextModels')} · {modelSummary({ ...catalog.defaults, ...selection })}</p>}
      </div>}
      <textarea aria-label="素材需求" value={state.draft} onChange={(event) => useMaterialWorkspace.setState({ draft: event.target.value })} placeholder={currentMessage ? '询问进度，或补充本轮制作要求…' : '描述素材需求，或选择画布产物提出修改…'} disabled={!value || state.loading} rows={2} onKeyDown={(event) => { if (event.key === 'Enter' && !event.nativeEvent.isComposing && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void send(); } }} />
      <footer className="material-composer-actions">
        {value && <MaterialReferenceImages projectId={value.projectId} selectedIds={state.referenceAssetIds} onChange={(referenceAssetIds) => useMaterialWorkspace.setState({ referenceAssetIds })} disabled={busy || state.loading} actionLabel="参考图片" />}
        {projectId && conversationId && <ModelPickerDialog
          key={conversationId}
          trigger={<button className="material-model-trigger" type="button" disabled={busy || state.loading} data-testid="material-model-picker"><SlidersHorizontal size={15} aria-hidden="true" />{t('title')}</button>}
          open={choosingModels}
          onOpenChange={(open) => {
            setModelDialogConversationId(open ? conversationId : null);
            if (open) setCatalogRequest((current) => current + 1);
          }}
          catalog={catalog}
          selection={selection}
          loading={catalogLoading}
          error={catalogError}
          onRetry={() => setCatalogRequest((current) => current + 1)}
          connectHref={getLocalizedPath(locale, `/editor?id=${encodeURIComponent(projectId)}&connectCodex=1`)}
          onApply={(nextSelection) => {
            if (value && isCurrentConversation(value)) useMaterialWorkspace.setState({ modelDraft: { conversationId, selection: nextSelection } });
          }}
        />}
        <button aria-label={currentMessage ? '发送素材消息' : '发送素材需求'} type="submit" className="material-primary material-send" disabled={busy || state.loading || !state.draft.trim() || !value || (!currentMessage && !canStartProduction)}>发送</button>
      </footer>
      <div className="material-composer-note">
        <p className="material-composer-hint">{currentMessage ? '消息接在当前任务上' : '⌘ / Ctrl + Enter 发送'}</p>
        {currentMessage && canStartProduction && <button type="button" disabled={busy || state.loading || !state.draft.trim()} onClick={() => { void send('start'); }}>开始新一轮制作</button>}
      </div>
    </form>
  </section>;
}
