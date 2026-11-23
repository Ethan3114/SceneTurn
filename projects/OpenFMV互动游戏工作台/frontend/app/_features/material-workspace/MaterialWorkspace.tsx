'use client';

import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import dynamic from 'next/dynamic';
import { ArrowLeft, ArrowRight, Check, Clock, Expand, Info, MessageSquare, Minus, MousePointer2, Plus } from 'lucide-react';
import { z } from 'zod';

import { agentStatuses } from '@/app/_utils/agentApi';
import { assetContentUrl, getGenerations, type GenerationView } from '@/app/_utils/cloudApi';
import { getProductionMessage, type MaterialNode } from '@/app/_utils/materialApi';
import type { MaterialCanvasHandle } from '@/app/_components/workbench/StudioPage';
import { taskLabels } from '@/app/_features/creation-canvas/GenerationTaskCard';
import { useResolvedMediaSrc } from '@/app/_hooks/useResolvedMediaSrc';
import { materialRoles, useMaterialWorkspace } from './store';
import MediaCandidates from './MediaCandidates';
import './materials.css';
import './material-roles.css';

const ManualCanvas = dynamic(() => import('@/app/_components/workbench/StudioPage'), { ssr: false });
const imageStatuses: Record<string, string> = { awaiting_confirmation: '等待生成确认', queued: '正在排队', claimed: '正在准备生成', waiting_connection: '等待个人 Codex 连接', submitting: '正在生成图片', response_saved: '正在保存图片', ready: '已完成', failed: '生成失败，请查看对话', submission_unknown: '请求状态待核实', cancelled: '已取消' };
const roleOrder = ['writer', 'scene_designer', 'character_designer', 'storyboarder', 'media_producer', 'sound_designer'];
const outputRoles = roleOrder.map((id) => materialRoles.find((role) => role.id === id)!);
const statusesInProgress = new Set(['queued', 'running', 'waiting_confirmation']);
const videoReceipt = z.object({ tool: z.literal('submit_generation'), result: z.object({ generation: z.object({ id: z.string() }) }) });
const videoBoundary = z.object({ frames: z.array(z.object({ type: z.enum(['first_frame', 'last_frame']), assetId: z.string() })) });
const shotSummary = z.object({ title: z.string(), action: z.string(), camera: z.string(), sound: z.string() });

function RoleStatus({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'good' | 'warn' | 'neutral' }) {
  const Icon = tone === 'good' ? Check : tone === 'warn' ? Clock : Info;
  return <span className="material-role-status" data-tone={tone}><Icon size={13} aria-hidden="true" />{children}</span>;
}

function Artifact({ node, showTitle, status }: { node: MaterialNode; showTitle: boolean; status?: ReactNode }) {
  const revision = useMaterialWorkspace((state) => state.conversation?.revision);
  const select = useMaterialWorkspace((state) => state.select);
  const selected = useMaterialWorkspace((state) => state.references.some((item) => item.nodeId === node.id));
  const [expanded, setExpanded] = useState(false);
  const metadata = node.metadata || {};
  const src = useResolvedMediaSrc(typeof metadata.storageKey === 'string' ? assetContentUrl(metadata.storageKey) : typeof metadata.assetId === 'string' ? assetContentUrl(metadata.assetId) : typeof metadata.src === 'string' ? metadata.src : node.type !== 'text' && typeof metadata.content === 'string' ? metadata.content : '');
  const content = node.type === 'text' && typeof metadata.content === 'string' ? metadata.content : '';
  const frameKeys = new Set(node.delivery?.segments.flatMap(({ firstFrameKey, lastFrameKey }) => [firstFrameKey, lastFrameKey]));
  const frameOrder = new Map([...frameKeys].map((key, index) => [key, index]));
  const images = [
    ...(node.delivery?.reusedFrames.map((image) => ({ ...image, id: image.frameKey, status: 'ready', errorCode: null, reused: true })) ?? []),
    ...(node.delivery?.images.map((image) => ({ ...image, reused: false })) ?? []),
  ];
  images.sort((a, b) => (frameOrder.get(a.frameKey || '') ?? frameOrder.size) - (frameOrder.get(b.frameKey || '') ?? frameOrder.size));
  const title = node.title || '素材';
  return <article className="material-role-artifact" data-testid="material-artifact" data-node-id={node.id} data-referenced={selected}>
    {showTitle && <h4>{title}</h4>}
    {node.type === 'image' && src && <img src={src} alt={node.title || '图片素材'} loading="lazy" />}
    {node.type === 'video' && src && <video src={src} controls preload="metadata" playsInline />}
    {node.type === 'audio' && src && <audio src={src} controls preload="metadata" />}
    {images.length > 0 && <div className="material-role-media">
      {images.map((image) => <figure className="material-image-result" key={image.id} data-image-job={image.reused ? undefined : image.id}>
        {image.assetId && image.status === 'ready' ? <img src={assetContentUrl(image.assetId)} alt={image.label} width={image.width} height={image.height} loading="lazy" /> : <div className="material-role-frame-state" role="status">{image.status === 'response_saved' && image.errorCode === 'IMAGE_REGISTRATION_FAILED' ? '图片已生成，登记失败，等待恢复' : imageStatuses[image.status] || '等待图片成果'}</div>}
        <figcaption title={`${image.label} · ${image.width} × ${image.height}`}>{image.label}{image.reused && ' · 复用原图'}</figcaption>
      </figure>)}
    </div>}
    {!!node.delivery?.segments.length && <ol className="material-plan-list">{node.delivery.segments.map((segment) => <li key={segment.key}>{segment.key} · {segment.durationSeconds} 秒：{segment.firstFrameKey} → {segment.lastFrameKey}</li>)}</ol>}
    {content && <div className="material-role-copy" data-expanded={expanded}>{content}</div>}
    {!content && !src && !images.length && <p className="material-role-waiting">{node.delivery?.kind === 'audio' ? '等待声音素材' : node.delivery?.kind === 'images' ? '等待图片成果' : '等待视频生成结果'}</p>}
    <footer className="material-role-artifact-actions">
      {status}
      <button type="button" aria-label={`引用到对话：${title}`} aria-pressed={selected} disabled={revision === undefined} onClick={() => { if (revision !== undefined) select({ nodeId: node.id, contentHash: node.contentHash, title, revision }); }}>
        {selected ? '已引用' : '引用'}
      </button>
      {content && <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起正文' : '展开正文'}</button>}
    </footer>
  </article>;
}

function VideoReview({ task, onBack }: { task: GenerationView; onBack: () => void }) {
  const awaiting = task.status === 'awaiting_confirmation';
  const boundaries = videoBoundary.safeParse(task.input.boundaryFrames).data;
  const summary = shotSummary.safeParse(task.input.shotSummary).data;
  const specs = [summary?.title, task.input.model, task.input.resolution, `${task.input.duration} 秒`, typeof task.input.ratio === 'string' && task.input.ratio].filter(Boolean).join(' · ');
  return <div className="material-video-review-viewport">
    <article className="material-video-review-view" data-testid="material-video-review-view" data-video-id={task.id}>
      <header>
        <h3>核对本段视频</h3>
        <RoleStatus tone={awaiting ? 'warn' : task.status === 'ready' ? 'good' : 'neutral'}>{awaiting ? '等待确认' : taskLabels[task.status] || task.status}</RoleStatus>
      </header>
      <p className="material-video-review-specs">{specs}</p>
      {boundaries && boundaries.frames.length > 0 && <div className="material-video-review-frames">
        {boundaries.frames.map((frame) => <figure key={frame.type}>
          <img src={assetContentUrl(frame.assetId)} alt={frame.type === 'first_frame' ? '视频首帧' : '视频尾帧'} />
          <figcaption>{frame.type === 'first_frame' ? '首帧' : '尾帧'}</figcaption>
        </figure>)}
      </div>}
      {summary && <dl className="material-video-review-details">
        <dt>动作与互动</dt><dd>{summary.action}</dd>
        <dt>运镜</dt><dd>{summary.camera}</dd>
        <dt>声音</dt><dd>{summary.sound}</dd>
      </dl>}
      <footer>
        <button type="button" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />返回素材制作</button>
        {awaiting && <button type="button" className="material-role-primary" onClick={() => useMaterialWorkspace.getState().openVideoReview(task.id)}>去确认生成<ArrowRight size={16} aria-hidden="true" /></button>}
      </footer>
    </article>
  </div>;
}

export default function MaterialWorkspace({ projectId, sceneId, saveRef, scenePicker, showChatButton = true }: {
  projectId: string;
  sceneId: string;
  saveRef: RefObject<MaterialCanvasHandle | null>;
  scenePicker?: ReactNode;
  showChatButton?: boolean;
}) {
  const value = useMaterialWorkspace((state) => state.conversation);
  const loading = useMaterialWorkspace((state) => state.loading);
  const error = useMaterialWorkspace((state) => state.error);
  const open = useMaterialWorkspace((state) => state.open);
  const videoResults = useMaterialWorkspace((state) => state.videoResults);
  const videoReviewTaskId = useMaterialWorkspace((state) => state.videoReviewTaskId);
  const [manual, setManual] = useState(false);
  const [zoom, setZoom] = useState(1);
  const manualRef = useRef<MaterialCanvasHandle>(null);
  const conversationId = value?.id;
  const latestMessage = getProductionMessage(value);
  const run = latestMessage?.run;

  useEffect(() => {
    void open({ projectId, sceneId });
    return () => useMaterialWorkspace.getState().close();
  }, [projectId, sceneId, open]);
  useEffect(() => {
    const save = async () => { if (manual) await manualRef.current?.save(); };
    saveRef.current = { save };
    useMaterialWorkspace.setState({ savePending: save });
    return () => { saveRef.current = null; useMaterialWorkspace.setState({ savePending: async () => {} }); };
  }, [manual, saveRef]);
  useEffect(() => {
    if (manual || !conversationId) return;
    let pending = false;
    const timer = window.setInterval(() => {
      if (pending) return;
      pending = true;
      void useMaterialWorkspace.getState().refresh().catch((problem: Error) => useMaterialWorkspace.setState({ error: problem.message })).finally(() => { pending = false; });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [manual, conversationId]);

  useEffect(() => {
    if (!run || !conversationId) return;
    const ids = new Set(run.steps.flatMap((step) => {
      if (!Array.isArray(step.toolResults)) return [];
      return step.toolResults.flatMap((receipt) => {
        const parsed = videoReceipt.safeParse(receipt);
        return parsed.success ? [parsed.data.result.generation.id] : [];
      });
    }));
    if (!ids.size) return;
    let cancelled = false;
    void getGenerations(projectId).then(({ generations }) => {
      if (cancelled || useMaterialWorkspace.getState().conversation?.id !== conversationId) return;
      const tasks = generations.filter((task) => task.sceneId === sceneId && (ids.has(task.id) || (task.retryOf && ids.has(task.retryOf))));
      useMaterialWorkspace.setState({ videoResults: { runId: run.id, tasks } });
    }).catch((problem: Error) => { if (!cancelled) useMaterialWorkspace.setState({ error: problem.message }); });
    return () => { cancelled = true; };
  }, [conversationId, projectId, run, sceneId]);

  async function switchManual() {
    if (manual) await manualRef.current?.save();
    setManual(!manual);
    if (manual) await useMaterialWorkspace.getState().refresh();
  }
  const brief = latestMessage?.directorBrief ?? value?.directorBrief;
  const steps = run?.steps ?? [];
  const tasks = videoResults?.runId === run?.id ? videoResults?.tasks : undefined;
  const pendingVideo = tasks?.find((task) => task.status === 'awaiting_confirmation');
  const reviewTask = tasks?.find((task) => task.id === videoReviewTaskId);
  const closeVideoReview = () => useMaterialWorkspace.setState({ videoReviewTaskId: null });
  const status = pendingVideo ? '等待视频确认' : brief?.statusText || (run && (agentStatuses[run.status] || run.status));
  const roles = outputRoles.filter((role) => role.id !== 'sound_designer' || value?.nodes.some((node) => node.producerRole === role.id) || steps.some((step) => step.role === role.id));
  return <section className="material-workspace" data-testid="material-workspace" data-scene-picker={Boolean(scenePicker)}>
    <header className="material-workspace-toolbar" data-testid="material-scene-toolbar">
      {scenePicker ?? <strong>素材制作</strong>}
      <div className="material-actions">
        {showChatButton && <button type="button" onClick={() => useMaterialWorkspace.setState({ chatOpen: true })}><MessageSquare size={15} aria-hidden="true" />问问 AI</button>}
        {!manual && reviewTask ? <button type="button" onClick={closeVideoReview}><ArrowLeft size={16} aria-hidden="true" />返回素材制作</button> : <button type="button" disabled={!manual && ['queued', 'running', 'stopping'].includes(run?.status || '')} onClick={() => { void switchManual().catch((problem: Error) => useMaterialWorkspace.setState({ error: problem.message })); }}>
          {manual ? <ArrowLeft size={16} aria-hidden="true" /> : <MousePointer2 size={16} aria-hidden="true" />}
          {manual ? '返回岗位展示' : '手动编辑画布'}
        </button>}
      </div>
    </header>
    {error && <p role="alert" className="material-error">{error}</p>}
    {loading && <p role="status" className="material-empty">正在读取素材与对话…</p>}
    {manual ? <div className="material-manual"><ManualCanvas projectId={projectId} initialSceneId={sceneId} embedded saveRef={manualRef} /></div> : reviewTask ? <VideoReview task={reviewTask} onBack={closeVideoReview} /> : <>
      <div className="material-canvas-viewport" data-testid="material-role-canvas"><div className="material-role-grid" style={{ zoom }}>
        <section className="material-role-area material-director-brief" data-role="director" data-testid="material-director-brief">
          <header className="material-role-card-header">
            <h3>总编 · 本轮制作安排</h3>
            {status && <RoleStatus tone={run?.status === 'completed' ? 'good' : run?.status === 'waiting_confirmation' ? 'warn' : 'neutral'}>{status}</RoleStatus>}
          </header>
          <p className="material-director-objective">{brief?.objective ?? (run ? '正在梳理本轮制作需求。' : '在「问问 AI」描述想制作的素材。')}</p>
          {steps.length > 0 && <details className="material-director-progress"><summary>查看制作进度</summary>
            <ol className="material-plan-list material-director-steps">{steps.map((step, index) => (
              <li key={step.id || index}><span>{materialRoles.find((entry) => entry.id === step.role)?.title}</span><strong>{step.title}</strong><span className="material-subtle">{agentStatuses[step.status || ''] || step.status}</span></li>
            ))}</ol>
          </details>}
          {value?.nodes.filter((node) => node.producerRole === 'director').map((node) => <Artifact key={node.id} node={node} showTitle />)}
        </section>
        {roles.map((role) => {
          const nodes = value?.nodes.filter((node) => node.producerRole === role.id) || [];
          const active = steps.some((step) => step.role === role.id && statusesInProgress.has(step.status || ''));
          const completed = steps.some((step) => step.role === role.id && step.status === 'completed');
          const video = role.id === 'media_producer' ? pendingVideo : undefined;
          const videoTitle = video ? shotSummary.safeParse(video.input.shotSummary).data?.title : undefined;
          const roleTitle = nodes[0]?.title || videoTitle;
          return <section key={role.id} className="material-role-area" data-role={role.id}>
            <header className="material-role-card-header"><h3>{role.title}{roleTitle && ` · ${roleTitle}`}</h3></header>
            {nodes.map((node, index) => <Artifact key={node.id} node={node} showTitle={index > 0} status={index === 0 && role.id === 'writer' && completed ? <RoleStatus tone="good">已采用</RoleStatus> : undefined} />)}
            {video ? <div className="material-role-video-review">
              <RoleStatus tone="warn">等待确认</RoleStatus>
              <p>{[video.input.model, video.input.resolution, `${video.input.duration} 秒`].filter(Boolean).join(' · ')}</p>
              <button type="button" className="material-role-primary" onClick={() => useMaterialWorkspace.getState().openVideoReview(video.id)}><ArrowRight size={16} aria-hidden="true" />核对视频镜头</button>
            </div> : nodes.length === 0 && <p className="material-role-waiting">{active ? '正在制作' : '等待制作安排'}</p>}
            {active && ['character_designer', 'scene_designer', 'storyboarder'].includes(role.id) && value && !value.capabilities.image && <p className="material-capability">请检查图片模型的服务连接</p>}
            {role.id === 'media_producer' && value && <MediaCandidates key={value.id} projectId={projectId} sceneId={sceneId} conversationId={value.id} />}
          </section>;
        })}
      </div></div>
      <footer className="material-canvas-controls"><div className="material-actions"><button type="button" aria-label="缩小素材画布" onClick={() => setZoom(Math.max(.5, zoom - .1))}><Minus size={15} /></button><span>{Math.round(zoom * 100)}%</span><button type="button" aria-label="放大素材画布" onClick={() => setZoom(Math.min(1.5, zoom + .1))}><Plus size={15} /></button><button type="button" aria-label="重置素材画布" onClick={() => setZoom(1)}><Expand size={15} /></button></div></footer>
    </>}
  </section>;
}
