'use client';

import { useEffect, useState } from 'react';
import { api, assetContentUrl } from '@/app/_utils/cloudApi';
import type { CanvasNodeData } from './upstream/canvas';

type Version = { revision: number; node: CanvasNodeData };
type Page = { versions: Version[]; before: number | null };
export function CanvasVersionHistory({ projectId, sceneId, nodeId }: { projectId: string; sceneId: string; nodeId: string }) {
  const [versions, setVersions] = useState<Version[]>([]);
  const [more, setMore] = useState<number | null>(null);
  const [cursor, setCursor] = useState<number>();
  const [chosen, setChosen] = useState<number>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    const path = `/projects/${encodeURIComponent(projectId)}/canvases/${encodeURIComponent(sceneId)}/nodes/${encodeURIComponent(nodeId)}/versions`;
    void api<Page>(cursor ? `${path}?before=${cursor}` : path, { signal: controller.signal }).then(page => {
      if (controller.signal.aborted) return;
      setVersions(old => cursor ? [...old,...page.versions] : page.versions); setMore(page.before); setLoading(false);
    }).catch((problem: Error) => { if (!controller.signal.aborted) { setError(problem.message); setLoading(false); } });
    return () => controller.abort();
  }, [projectId, sceneId, nodeId, cursor]);
  const entry = versions.find(v => v.revision === chosen) ?? versions[0];
  const source = entry?.node.metadata?.storageKey ? assetContentUrl(entry.node.metadata.storageKey) : undefined;
  return <div className="fmv-version-list">{loading && <p role="status">正在读取已保存版本…</p>}{error && <p role="alert">{error}</p>}
    {versions.length > 0 && <label>已保存版本<select value={entry?.revision} onChange={e => setChosen(Number(e.target.value))}>{versions.map(v => <option value={v.revision} key={v.revision}>版本 {v.revision} · {v.node.title}</option>)}</select></label>}
    {entry && <article><h3>{entry.node.title}</h3>{source && (entry.node.type === 'image' ? <img src={source} alt={entry.node.title}/> : entry.node.type === 'video' ? <video src={source} controls/> : entry.node.type === 'audio' ? <audio src={source} controls/> : null)}
      <p>{entry.node.metadata?.composerContent || entry.node.metadata?.prompt || (entry.node.type === 'text' && entry.node.metadata?.content)}</p></article>}
    {!loading && !error && !entry && <p>保存画布后可以查看此节点的版本。</p>}
    {more && <button disabled={loading} onClick={() => { setLoading(true); setCursor(more); }}>查看更早版本</button>}</div>;
}
