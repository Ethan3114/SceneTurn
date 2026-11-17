'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useLocale } from 'next-intl';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { operationId } from '@/app/_utils/cloudApi';
import { confirmTransfer, listTransfers, prepareExport, retryTransfer, uploadProjectArchive, type ProjectTransfer } from '@/app/_utils/transferApi';
import './transfers.css';

const labels = { queued: '等待处理', processing: '正在处理', awaiting_confirmation: '等待确认', blocked: '需要补充文件或处理冲突', ready: '已完成', failed: '处理失败' };

export default function ProjectTransfers({ projectId, onImported }: { projectId?: string; onImported?: () => void }) {
  const locale = useLocale();
  const [jobs, setJobs] = useState<ProjectTransfer[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const uploaded = useRef<{ file: File; operation: string } | null>(null);
  const pendingExport = useRef<{ key: string; operation: string } | null>(null);
  const reported = useRef(new Set<string>());
  const callback = useRef(onImported);
  callback.current = onImported;
  async function refresh() { setJobs(await listTransfers(projectId)); }
  useEffect(() => {
    let cancelled = false, pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try { const next = await listTransfers(projectId); if (!cancelled) setJobs(next); }
      catch (problem) { if (!cancelled) setError(problem instanceof Error ? problem.message : '项目记录读取失败'); }
      finally { pending = false; }
    }
    void load();
    const timer = window.setInterval(() => { void load(); }, 2000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [projectId]);
  useEffect(() => {
    for (const job of jobs) if (job.kind === 'import' && job.status === 'ready' && !reported.current.has(job.id)) {
      reported.current.add(job.id); callback.current?.();
    }
  }, [jobs]);
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); await refresh(); }
    catch (problem) { setError(problem instanceof Error ? problem.message : '项目操作失败'); }
    finally { setBusy(false); }
  }
  async function startExport(kind: 'backup' | 'game') {
    if (!projectId) throw new Error('请打开需要导出的项目');
    const session = useProjectSessionStore.getState();
    if (session.projectId !== projectId) throw new Error('当前编辑项目已变化，请重新打开导出窗口');
    const saved = await session.saveNow();
    if (!saved) throw new Error('项目尚未保存，导出已终止');
    const revision = saved.metadata.serverRevision;
    if (typeof revision !== 'number' || !Number.isInteger(revision) || revision < 1) throw new Error('当前项目缺少已保存版本');
    const key = `${projectId}:${revision}:${kind}`;
    const request = pendingExport.current?.key === key ? pendingExport.current : { key, operation: operationId() };
    pendingExport.current = request;
    await prepareExport(projectId, revision, kind, request.operation);
    pendingExport.current = null;
  }
  return <section className="fmv-transfers" data-testid="project-transfers">
    <div className="fmv-transfer-actions">
      {!projectId && <label className="fmv-transfer-upload">导入完整项目<input data-testid="project-import-file" type="file" accept=".zip,application/zip" disabled={busy} onChange={(event) => {
        const file = event.target.files?.[0];
        if (file) void perform(async () => {
          const request = uploaded.current?.file === file ? uploaded.current : { file, operation: operationId() };
          uploaded.current = request; await uploadProjectArchive(file, request.operation); uploaded.current = null;
        });
      }} /></label>}
      {projectId && <><button data-testid="project-backup-prepare" type="button" disabled={busy} onClick={() => { void perform(() => startExport('backup')); }}>备份完整项目</button>
        <button data-testid="game-export-prepare" type="button" disabled={busy} onClick={() => { void perform(() => startExport('game')); }}>生成网页可玩版本</button></>}
    </div>
    <p className="fmv-transfer-note">{projectId ? '可玩版本完成后，点击“在网页游玩”直接播放。项目备份包含全部素材、对话及创作历史。' : '支持OpenFMV完整项目ZIP、无限画布version 3完整导出及本工作台项目备份。检查后确认导入。'}</p>
    {error && <p role="alert">{error}</p>}
    {jobs.map((job) => <article key={job.id} className="fmv-transfer-job" data-transfer-id={job.id} data-transfer-status={job.status}>
      <div className="fmv-transfer-heading"><strong>{job.kind === 'import' ? '项目导入' : job.kind === 'backup' ? '完整项目备份' : '私有游戏版本'}{job.sourceRevision ? ` · v${job.sourceRevision}` : ''}</strong><span>{labels[job.status]}</span></div>
      {job.projects.map((project) => <p key={project.id}>{project.title} · {project.sceneCount}个场景 · {project.canvasNodeCount}个素材节点 · {project.assetCount}项媒体</p>)}
      {job.issues.length > 0 && <ul>{job.issues.map((issue, index) => <li key={`${issue.code}:${index}`}>{issue.message}{issue.sourceId ? `（${issue.sourceId}）` : ''}</li>)}</ul>}
      {job.errorMessage && <p role="alert">{job.errorMessage}</p>}
      {job.kind !== 'import' && ['failed', 'ready'].includes(job.status) && <details><summary>修复文件后重新生成此版本</summary><p>使用本次保存的内容重新生成，已有文件和记录保留。</p><button type="button" disabled={busy} onClick={() => { void perform(async () => { await retryTransfer(job); }); }}>重新生成此版本</button></details>}
      {job.mappings.length > 0 && <details><summary>来源与目标映射（{job.mappings.length}项）</summary><div className="fmv-transfer-mappings">{job.mappings.map((mapping, index) => <p key={`${mapping.kind}:${index}`}><span>{mapping.kind}</span><code>{mapping.sourceId}</code><span>→</span><code>{mapping.targetId}</code></p>)}</div></details>}
      {job.status === 'awaiting_confirmation' && <button data-testid="transfer-confirm" type="button" disabled={busy} onClick={() => { void perform(async () => { await confirmTransfer(job); }); }}>{job.kind === 'import' ? `确认导入${job.projects.length}个项目` : `确认生成v${job.sourceRevision}可玩版本`}</button>}
      {job.status === 'ready' && job.kind === 'import' && <div className="fmv-transfer-actions">{job.projects.map((project) => <Link key={project.id} href={`/zh-CN/editor?id=${encodeURIComponent(project.id)}`}>打开{project.title}</Link>)}</div>}
      {job.previewUrl && job.projectId && <Link data-testid="transfer-preview" href={getLocalizedPath(locale, `/play/${encodeURIComponent(job.projectId)}?version=${encodeURIComponent(job.id)}`)}>在网页游玩 · v{job.sourceRevision}</Link>}
      {job.downloadUrl && (job.kind === 'backup' ? <a data-testid="transfer-download" href={job.downloadUrl}>下载项目备份</a> : <details><summary>下载游戏包</summary><a data-testid="transfer-download" href={job.downloadUrl}>下载游戏包</a></details>)}
    </article>)}
    {jobs.length === 0 && <p className="fmv-transfer-note">本次操作和完成记录会保存在这里。</p>}
  </section>;
}
