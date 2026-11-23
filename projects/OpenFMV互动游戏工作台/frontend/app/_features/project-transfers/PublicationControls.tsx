'use client';

import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import Link from 'next/link';
import { useLocale } from 'next-intl';
import { z } from 'zod';

import { api, type ProjectEnvelope } from '@/app/_utils/cloudApi';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import type { OpenFMVAsset } from '@/app/_types';

const resultSchema = z.object({ publication: z.object({
  slug: z.string(), published: z.boolean(), title: z.string(), description: z.string(),
  posterAssetId: z.string().nullable(), trailerAssetId: z.string().nullable(),
}).nullable() });
type Publication = z.infer<typeof resultSchema>['publication'];

export default function PublicationControls({ id, projectId, initialTitle }: { id: string; projectId: string; initialTitle: string }) {
  const [open, setOpen] = useState(false);
  return <details onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>作品馆发布设置</summary>
    {open && <PublicationForm id={id} projectId={projectId} initialTitle={initialTitle} />}
  </details>;
}

function PublicationForm({ id, projectId, initialTitle }: { id: string; projectId: string; initialTitle: string }) {
  const locale = useLocale();
  const path = `/transfers/${encodeURIComponent(id)}/publication`;
  const [publication, setPublication] = useState<Publication>(null);
  const [assets, setAssets] = useState<OpenFMVAsset[]>([]);
  const [title, setTitle] = useState(initialTitle);
  const [description, setDescription] = useState('');
  const [poster, setPoster] = useState('');
  const [trailer, setTrailer] = useState('');
  const [loading, setLoading] = useState(true);
  const [available, setAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    void Promise.all([api(path), api<ProjectEnvelope>(`/projects/${encodeURIComponent(projectId)}`)]).then(([value, { project }]) => {
      if (!active) return;
      const stored = resultSchema.parse(value).publication;
      setPublication(stored);
      setAssets(project.assets);
      if (stored) {
        setTitle(stored.title);
        setDescription(stored.description);
        setPoster(stored.posterAssetId ?? '');
        setTrailer(stored.trailerAssetId ?? '');
      }
      setAvailable(true);
    }).catch((problem: unknown) => {
      if (active) setError(problem instanceof Error ? problem.message : '无法读取发布状态');
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [path, projectId]);

  async function save(published: boolean) {
    const wording = published ? '确认向访客公开这个固定版本、所选封面和先导片？' : '确认从作品馆撤下这个固定版本？';
    if (busy || !available || !window.confirm(wording)) return;
    setBusy(true);
    setError('');
    try {
      const body = published ? { published, title, description, posterAssetId: poster || null, trailerAssetId: trailer || null } : { published };
      setPublication(resultSchema.parse(await api(path, { method: 'PUT', body: JSON.stringify(body) })).publication);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : '无法更新发布状态');
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void save(true);
  }

  if (loading) return <p role="status">正在读取发布状态…</p>;
  return <div className="fmv-publication-controls">
    <p role="status">{publication?.published ? '已公开，访客可直接游玩。' : '仅项目所有者可游玩。'}</p>
    {error && <p role="alert">{error}</p>}
    {available && <form onSubmit={submit}>
      <label>作品名称<input required maxLength={160} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      <label>作品简介<textarea maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} rows={2} /></label>
      <label>封面<select value={poster} onChange={(event) => setPoster(event.target.value)}><option value="">不选封面</option>{assets.filter(({ type }) => type === 'image').map((asset) => <option key={asset.id} value={asset.id}>{asset.name}</option>)}</select></label>
      <label>先导片<select value={trailer} onChange={(event) => setTrailer(event.target.value)}><option value="">不选先导片</option>{assets.filter(({ type }) => type === 'video').map((asset) => <option key={asset.id} value={asset.id}>{asset.name}</option>)}</select></label>
      <div className="fmv-transfer-actions">
        <button type="submit" disabled={busy || !title.trim()}>{publication?.published ? '更新公开信息' : '公开至作品馆'}</button>
        {publication?.published && <>
          <Link href={getLocalizedPath(locale, `/works/${encodeURIComponent(publication.slug)}`)}>查看公开作品</Link>
          <button type="button" disabled={busy} onClick={() => { void save(false); }}>撤下作品</button>
        </>}
      </div>
    </form>}
  </div>;
}
