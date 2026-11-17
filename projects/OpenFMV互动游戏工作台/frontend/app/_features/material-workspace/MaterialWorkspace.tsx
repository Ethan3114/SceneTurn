'use client';

import { useEffect, useRef, useState, type RefObject } from 'react';
import dynamic from 'next/dynamic';
import { Expand, MessageSquare, Minus, Plus } from 'lucide-react';
import { assetContentUrl } from '@/app/_utils/cloudApi';
import type { MaterialNode } from '@/app/_utils/materialApi';
import type { MaterialCanvasHandle } from '@/app/_components/workbench/StudioPage';
import { useResolvedMediaSrc } from '@/app/_hooks/useResolvedMediaSrc';
import { materialRoles, useMaterialWorkspace } from './store';
import MediaCandidates from './MediaCandidates';
import './materials.css';

const ManualCanvas = dynamic(() => import('@/app/_components/workbench/StudioPage'), { ssr: false });
const imageStatuses: Record<string, string> = { awaiting_confirmation: '等待生成确认', queued: '正在排队', claimed: '正在准备生成', submitting: '正在生成图片', response_saved: '正在保存图片', ready: '已完成', failed: '生成失败，请查看对话', submission_unknown: '请求状态待核实', cancelled: '已取消' };

function Artifact({ node }: { node: MaterialNode }) {
  const conversation = useMaterialWorkspace((state) => state.conversation);
  const select = useMaterialWorkspace((state) => state.select);
  const selected = useMaterialWorkspace((state) => state.references.some((item) => item.nodeId === node.id));
  const [expanded, setExpanded] = useState(false);
  const metadata = node.metadata || {};
  const src = useResolvedMediaSrc(typeof metadata.storageKey === 'string' ? assetContentUrl(metadata.storageKey) : typeof metadata.assetId === 'string' ? assetContentUrl(metadata.assetId) : typeof metadata.src === 'string' ? metadata.src : node.type !== 'text' && typeof metadata.content === 'string' ? metadata.content : '');
  const content = typeof metadata.content === 'string' ? metadata.content : '';
  return <article className={`material-artifact ${selected ? 'is-selected' : ''}`} data-testid="material-artifact" data-node-id={node.id}>
    <button className="material-artifact-title" onClick={() => { if (conversation) select({ nodeId: node.id, contentHash: node.contentHash, title: node.title || '素材', revision: conversation.revision }); }}>
      <span>{node.title || '素材'}</span><span className="material-subtle">{selected ? '已引用' : '引用到对话'}</span>
    </button>
    {node.type === 'image' && src && <img src={src} alt={node.title || '图片素材'} loading="lazy" />}
    {node.type === 'video' && src && <video src={src} controls preload="metadata" playsInline />}
    {node.type === 'audio' && src && <audio src={src} controls preload="metadata" />}
    {node.delivery?.images.map((image) => <figure className="material-image-result" key={image.id} data-image-job={image.id}>
      {image.assetId && image.status === 'ready' ? <img src={assetContentUrl(image.assetId)} alt={image.label} loading="lazy" /> : <div className="material-role-empty" role="status">{imageStatuses[image.status] || '等待图片成果'}</div>}
      <figcaption>{image.label} · {image.width} × {image.height}</figcaption>
    </figure>)}
    {!!node.delivery?.segments.length && <ol className="material-plan-list">{node.delivery.segments.map((segment) => <li key={segment.key}>{segment.key} · {segment.durationSeconds} 秒：{segment.firstFrameKey} → {segment.lastFrameKey}</li>)}</ol>}
    {content && <div className={expanded ? 'material-artifact-body expanded' : 'material-artifact-body'}>{content}</div>}
    {!content && !src && !node.delivery?.images.length && <p className="material-subtle">{node.delivery?.kind === 'audio' ? '等待声音素材' : node.delivery?.kind === 'images' ? '等待图片成果' : '等待视频生成结果'}</p>}
    {content && <button className="material-text-button" onClick={() => setExpanded(!expanded)}>{expanded ? '收起正文' : '展开正文'}</button>}
  </article>;
}

export default function MaterialWorkspace({ projectId, sceneId, saveRef }: { projectId: string; sceneId: string; saveRef: RefObject<MaterialCanvasHandle | null> }) {
  const value = useMaterialWorkspace((state) => state.conversation);
  const loading = useMaterialWorkspace((state) => state.loading);
  const error = useMaterialWorkspace((state) => state.error);
  const open = useMaterialWorkspace((state) => state.open);
  const [manual, setManual] = useState(false);
  const [zoom, setZoom] = useState(1);
  const manualRef = useRef<MaterialCanvasHandle>(null);
  const conversationId = value?.id;

  useEffect(() => {
    void open({ projectId, sceneId });
    return () => useMaterialWorkspace.getState().close();
  }, [projectId, sceneId, open]);
  useEffect(() => {
    const save = async () => { if (manual) await manualRef.current?.save(); };
    saveRef.current = { save };
    useMaterialWorkspace.setState({ savePending: async () => { await save(); setManual(false); } });
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

  async function switchManual() {
    if (manual) await manualRef.current?.save();
    setManual(!manual);
    if (manual) await useMaterialWorkspace.getState().refresh();
  }
  const roles = materialRoles.filter((role) => role.id !== 'sound_designer' || value?.nodes.some((node) => node.producerRole === role.id));
  return <section className="material-workspace" data-testid="material-workspace">
    <header className="material-workspace-toolbar"><div><strong>素材制作</strong><span className="material-subtle">与总监对话，产物按岗位展示</span></div><div className="material-actions">
      <button onClick={() => useMaterialWorkspace.setState({ chatOpen: true })}><MessageSquare size={15} />问问 AI</button>
      <button disabled={['queued', 'running', 'stopping'].includes(value?.messages.at(-1)?.run.status || '')} onClick={() => { void switchManual().catch((problem: Error) => useMaterialWorkspace.setState({ error: problem.message })); }}>{manual ? '返回岗位展示' : '手动编辑画布'}</button>
    </div></header>
    {error && <p role="alert" className="material-error">{error}</p>}
    {loading && <p role="status" className="material-empty">正在读取素材与对话…</p>}
    {manual ? <div className="material-manual"><ManualCanvas projectId={projectId} initialSceneId={sceneId} embedded saveRef={manualRef} /></div> : <>
      <div className="material-canvas-viewport" data-testid="material-role-canvas"><div className="material-role-grid" style={{ zoom }}>
        {roles.map((role) => {
          const nodes = value?.nodes.filter((node) => node.producerRole === role.id) || [];
          const steps = value?.messages.at(-1)?.run.steps.filter((step) => step.role === role.id) || [];
          const active = steps.some((step) => ['running', 'queued', 'waiting_confirmation'].includes(step.status || ''));
          return <section key={role.id} className="material-role-area" data-role={role.id} style={{ borderTopColor: role.color }}><header><span style={{ color: role.color }}>●</span><strong>{role.title}</strong><span className="material-subtle">{active ? '处理中' : `${nodes.length} 份产物`}</span></header>
            <p className="material-subtle">{role.description}</p>
            {role.id === 'director' && !!value?.messages.at(-1)?.run.plan.length && <article className="material-artifact"><div className="material-artifact-body expanded"><strong>本轮制作安排</strong><p>{value.messages.at(-1)?.content}</p><ol className="material-plan-list">{value.messages.at(-1)?.run.plan.map((item, index) => <li key={index}><span>{materialRoles.find((entry) => entry.id === item.role)?.title}</span><strong>{item.title}</strong></li>)}</ol></div></article>}
            {nodes.length ? nodes.map((node) => <Artifact key={node.id} node={node} />) : !(role.id === 'director' && value?.messages.at(-1)?.run.plan.length) && <div className="material-role-empty">{active ? '岗位正在处理，产物会保存到这里' : role.id === 'director' ? '在“问问 AI”中描述需求，总监会梳理制作安排' : '收到总监安排后，在这里展示产物'}</div>}
            {['character_designer', 'scene_designer', 'storyboarder'].includes(role.id) && value && !value.capabilities.image && <p className="material-capability">请配置商汤图片服务</p>}
            {role.id === 'media_producer' && value && <MediaCandidates key={value.id} projectId={projectId} sceneId={sceneId} conversationId={value.id} />}
          </section>;
        })}
      </div></div>
      <footer className="material-canvas-controls"><span className="material-subtle">点击产物标题，引用到对话</span><div className="material-actions"><button aria-label="缩小素材画布" onClick={() => setZoom(Math.max(.5, zoom - .1))}><Minus size={15} /></button><span>{Math.round(zoom * 100)}%</span><button aria-label="放大素材画布" onClick={() => setZoom(Math.min(1.5, zoom + .1))}><Plus size={15} /></button><button aria-label="重置素材画布" onClick={() => setZoom(1)}><Expand size={15} /></button></div></footer>
    </>}
  </section>;
}
