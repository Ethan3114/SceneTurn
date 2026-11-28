'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useLocale } from 'next-intl';

import { Button } from '@/app/_components/ui/button';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import type { SceneTurnAsset } from '@/app/_types';
import { getCodexConnections, type CodexConnection } from '@/app/_utils/agentApi';
import { operationId } from '@/app/_utils/cloudApi';
import { actOnCodexImage, createCodexImage, listCodexImages, type CodexImageJob } from '@/app/_utils/codexImageApi';
import { getLocalizedPath } from '@/app/_utils/localePaths';

const statusText: Record<CodexImageJob['status'], string> = {
  awaiting_confirmation: '等待确认', queued: '等待执行', running: '正在生成', waiting_connection: '等待 Codex 连接',
  submission_unknown: '等待取回', ready: '已入库', failed: '生成失败', stopped: '已停止',
};
const field = 'w-full rounded-lg border border-white/15 bg-[#13131e] p-2.5 text-sm text-white';

export default function CodexImagePanel({ projectId, assets, onAdopt }: {
  projectId: string;
  assets: SceneTurnAsset[];
  onAdopt: (assetId: string) => Promise<void>;
}) {
  const locale = useLocale();
  const [connections, setConnections] = useState<CodexConnection[]>([]);
  const [jobs, setJobs] = useState<CodexImageJob[] | null>(null);
  const [prompt, setPrompt] = useState('');
  const [refs, setRefs] = useState<string[]>([]);
  const [transparent, setTransparent] = useState(false);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const writing = useRef(false);
  const pending = useRef(new Map<string, string>());
  const online = connections.filter((connection) => connection.status === 'online' && connection.canGenerateImages);
  const connectionId = online.some(({ id }) => id === selected) ? selected : online[0]?.id;

  useEffect(() => {
    let active = true;
    let loading = false;
    async function poll() {
      if (loading || writing.current) return;
      loading = true;
      try {
        const [available, current] = await Promise.all([getCodexConnections(projectId), listCodexImages(projectId)]);
        if (active && !writing.current) { setConnections(available); setJobs(current); }
      } catch (problem) {
        if (active) setError(problem instanceof Error ? problem.message : '无法读取 Codex 连接');
      } finally {
        loading = false;
      }
    }
    void poll();
    const timer = window.setInterval(() => { void poll(); }, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [projectId]);

  async function perform(action: () => Promise<void>) {
    if (writing.current) return;
    writing.current = true;
    setError('');
    setBusy(true);
    try {
      await action();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : '操作失败');
    } finally {
      writing.current = false;
      setBusy(false);
    }
  }

  function remember(job: CodexImageJob) {
    setJobs((current) => current?.some(({ id }) => id === job.id)
      ? current.map((item) => item.id === job.id ? job : item)
      : [job, ...(current ?? [])]);
  }

  function requestId(key: string) {
    const id = pending.current.get(key) ?? operationId();
    pending.current.set(key, id);
    return id;
  }

  async function create() {
    const session = useProjectSessionStore.getState();
    if (session.projectId !== projectId) throw new Error('请重新打开当前项目');
    const project = await session.saveNow();
    if (!project || project.metadata.serverRevision === undefined) throw new Error('保存项目后再准备生成');
    const draft = { expectedRevision: project.metadata.serverRevision, prompt: prompt.trim(), referenceAssetIds: refs, transparentBackground: transparent };
    const key = JSON.stringify(draft);
    remember(await createCodexImage(projectId, { ...draft, operationId: requestId(key) }));
    pending.current.delete(key);
    setPrompt('');
    setRefs([]);
    setTransparent(false);
  }

  async function update(job: CodexImageJob, action: 'confirm' | 'stop') {
    if (action === 'confirm' && !connectionId) throw new Error('请连接支持生图的个人 Codex');
    const key = `${job.id}:${action}:${action === 'confirm' ? connectionId : ''}`;
    remember(await actOnCodexImage(projectId, job.id, action, requestId(key), connectionId));
    pending.current.delete(key);
  }

  return <section className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4" aria-label="个人 Codex 生图">
    <p className="text-sm text-white/70" role="status">{jobs === null ? '正在读取连接…' : online.length > 0 ? '个人 Codex 已连接，可使用个人账户额度。' : '当前没有在线且支持生图的连接。'}</p>
    {online.length === 0 && <Link href={getLocalizedPath(locale, `/settings/models?projectId=${encodeURIComponent(projectId)}`)} className="text-sm text-[#c4b7ff]">查看连接状态</Link>}
    {online.length > 1 && <label className="grid gap-1 text-sm">使用连接<select className={field} value={connectionId} onChange={(event) => setSelected(event.target.value)}>{online.map((item, index) => <option key={item.id} value={item.id}>连接 {index + 1} · {item.clientVersion ?? 'Codex'}</option>)}</select></label>}
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    <form className="grid gap-3" onSubmit={(event) => { event.preventDefault(); void perform(create); }}>
      <label className="grid gap-1 text-sm">画面说明<textarea className={field} rows={4} required maxLength={6000} value={prompt} onChange={(event) => setPrompt(event.target.value)} /></label>
      <label className="grid gap-1 text-sm">参考图片（可选，最多 5 张）
        <select className={field} multiple size={3} value={refs} onChange={(event) => setRefs(Array.from(event.target.selectedOptions, ({ value }) => value).slice(0, 5))}>
          {assets.filter(({ type }) => type === 'image').map(({ id, name }) => <option key={id} value={id}>{name}</option>)}
        </select>
      </label>
      <label className="flex gap-2 text-sm"><input type="checkbox" checked={transparent} onChange={(event) => setTransparent(event.target.checked)} />透明背景</label>
      <Button type="submit" variant="glass" disabled={busy || !connectionId || !prompt.trim()}>准备 Codex 图片</Button>
    </form>
    {jobs?.map((job) => <article key={job.id} className="space-y-2 rounded-xl border border-white/15 bg-white/5 p-3" data-testid="codex-image-job" data-status={job.status}>
      <p className="text-sm font-semibold" role="status">{statusText[job.status]}</p>
      <p className="whitespace-pre-wrap break-words text-sm text-white/75">{job.prompt}</p>
      {job.referenceAssets?.length ? <p className="text-xs text-white/65">参考图片 · {job.referenceAssets.map(({ name }) => name).join('、')}</p> : null}
      {job.error && <p className="text-sm text-red-300">{job.error.message}</p>}
      {job.status === 'awaiting_confirmation' && <Button type="button" variant="glass" disabled={busy || !connectionId} onClick={() => { void perform(() => update(job, 'confirm')); }}>确认使用个人 Codex 额度</Button>}
      {['awaiting_confirmation', 'queued', 'running', 'waiting_connection'].includes(job.status) && <Button type="button" variant="ghost" disabled={busy} onClick={() => { void perform(() => update(job, 'stop')); }}>停止</Button>}
      {job.asset && <div className="grid gap-2">
        {/* The source is the project's authenticated media endpoint. */}
        <img src={job.asset.url} alt={job.asset.name} loading="lazy" className="max-h-56 w-full rounded-lg object-contain" />
        <Button type="button" variant="glass" disabled={busy} onClick={() => { const asset = job.asset; if (asset) void perform(() => onAdopt(asset.id)); }}>在素材库查看</Button>
      </div>}
    </article>)}
  </section>;
}
