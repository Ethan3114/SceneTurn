'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { useLocale } from 'next-intl';
import { useRouter } from 'next/navigation';
import { ArrowLeft, ArrowRight, Film, FolderOpen, LayoutGrid, Play, Plus, Upload } from 'lucide-react';

import { api, assetContentUrl, createCloudProject, projectList, type ProjectEnvelope, type ProjectSummary } from '@/app/_utils/cloudApi';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/app/_components/ui/dialog';
import type { OpenFMVProject } from '@/app/_types';
import { WorkspaceShell, useWorkspaceUser, errorText } from './WorkspaceShell';
import ProjectTransfers from '@/app/_features/project-transfers/ProjectTransfers';
import './sceneturn-projects.css';

function projectPoster(project: OpenFMVProject) {
  const covers = project.assets.flatMap((asset) => {
    if (asset.type === 'image') return [assetContentUrl(asset.id)];
    const poster = `/api/v1/assets/${encodeURIComponent(asset.id)}/poster`;
    return asset.type === 'video' && asset.metadata?.poster === poster ? [poster] : [];
  });
  return covers.find((source) => source === project.metadata.coverImage) ?? covers[0];
}

function ProjectPoster({ source, size }: { source?: string; size: 'feature' | 'row' }) {
  const [failedSource, setFailedSource] = useState<string>();
  const available = source && source !== failedSource ? source : undefined;
  return <div className={`fmv-project-poster fmv-project-poster-${size}`} data-has-poster={Boolean(available)}>
    {available ? <Image src={available} alt="" fill unoptimized sizes={size === 'feature' ? '(max-width: 760px) 100vw, 54vw' : '111px'} onError={() => setFailedSource(available)} />
      : <Film size={size === 'feature' ? 32 : 20} aria-hidden="true" />}
  </div>;
}

export default function ProjectsPage({ view = 'list' }: { view?: 'list' | 'import' }) {
  const { user, error: userError } = useWorkspaceUser();
  const locale = useLocale();
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [featuredCover, setFeaturedCover] = useState<{ id: string; revision: number; source?: string } | null>(null);
  const [title, setTitle] = useState('');
  const [error, setError] = useState('');
  const [createError, setCreateError] = useState('');
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const createTrigger = useRef<HTMLButtonElement | null>(null);
  const featured = projects?.[0];
  const featuredId = featured?.id;
  const featuredRevision = featured?.revision;
  const coverSource = featuredCover?.id === featuredId && featuredCover?.revision === featuredRevision ? featuredCover?.source : undefined;

  const refreshProjects = useCallback(() => {
    void projectList().then((value) => { setProjects(value.projects); setError(''); }).catch((problem: unknown) => setError(errorText(problem)));
  }, []);

  useEffect(() => {
    if (!user || view !== 'list') return;
    let active = true;
    void projectList().then((value) => { if (active) setProjects(value.projects); })
      .catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    return () => { active = false; };
  }, [user, view]);

  useEffect(() => {
    if (!user || !featuredId || featuredRevision === undefined || view !== 'list') return;
    const request = new AbortController();
    void api<ProjectEnvelope>(`/projects/${encodeURIComponent(featuredId)}`, { signal: request.signal })
      .then(({ project }) => {
        if (!request.signal.aborted) setFeaturedCover({ id: featuredId, revision: featuredRevision, source: projectPoster(project) });
      })
      .catch(() => {
        if (!request.signal.aborted) setFeaturedCover({ id: featuredId, revision: featuredRevision });
      });
    return () => request.abort();
  }, [featuredId, featuredRevision, user, view]);

  const projectsPath = getLocalizedPath(locale, '/projects');
  const importPath = `${projectsPath}?view=import`;
  const editorPath = (id: string) => getLocalizedPath(locale, `/editor?id=${encodeURIComponent(id)}`);
  const playerPath = (id: string) => getLocalizedPath(locale, `/play/${encodeURIComponent(id)}`);

  function openCreate(event: React.MouseEvent<HTMLButtonElement>) {
    createTrigger.current = event.currentTarget;
    setTitle('');
    setCreateError('');
    setCreateOpen(true);
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    const name = title.trim();
    if (!name) { setCreateError('请输入项目名称。'); return; }
    setBusy(true);
    setCreateError('');
    try {
      const { project } = await createCloudProject(name);
      router.push(editorPath(project.id));
    } catch (problem: unknown) {
      setCreateError(errorText(problem));
      setBusy(false);
    }
  }

  if (!user) return <main className="fmv-loading">{userError || '正在读取账号…'}</main>;

  if (view === 'import') return <WorkspaceShell user={user} title="创作台" projectLibrary>
    <main className="fmv-project-import-page">
      <Link href={projectsPath} className="fmv-project-action"><ArrowLeft size={16} aria-hidden="true" />返回项目</Link>
      <section className="fmv-project-import-card" aria-labelledby="fmv-project-import-title">
        <h2 id="fmv-project-import-title">导入与恢复完整项目</h2>
        <p className="fmv-project-note">上传后核对项目、场景和素材，再确认导入。</p>
        <ProjectTransfers appearance="project-import" />
      </section>
    </main>
  </WorkspaceShell>;

  return <WorkspaceShell user={user} title="创作台" projectLibrary>
    <div className="fmv-project-library">
      <nav className="fmv-project-rail" aria-label="项目快捷入口">
        <Link href={projectsPath} aria-current="page"><LayoutGrid size={18} aria-hidden="true" />我的项目</Link>
        <Link href={getLocalizedPath(locale, '/assets')}><FolderOpen size={18} aria-hidden="true" />素材库</Link>
        <Link href={getLocalizedPath(locale, '/works')}><Film size={18} aria-hidden="true" />作品馆</Link>
      </nav>
      <main className="fmv-project-content">
        <header className="fmv-project-page-heading">
          <h2>我的项目</h2>
          <div className="fmv-project-actions">
            <Link href={importPath} className="fmv-project-action" data-testid="project-import-open"><Upload size={17} aria-hidden="true" />导入与恢复完整项目</Link>
            <button type="button" className="fmv-project-action fmv-project-dark" data-testid="project-new" onClick={openCreate}><Plus size={18} aria-hidden="true" />新建项目</button>
          </div>
        </header>
        {error && <p role="alert" className="fmv-project-error">{error} <button type="button" className="fmv-project-action" onClick={refreshProjects}>重新加载</button></p>}
        {!projects && !error && <p className="fmv-project-note fmv-project-loading" role="status">正在读取项目…</p>}
        {projects?.length === 0 && <section className="fmv-project-empty">
          <FolderOpen size={32} aria-hidden="true" />
          <h3>还没有项目</h3>
          <p className="fmv-project-note">从新的剧情开始，或导入一份完整项目备份。</p>
          <div className="fmv-project-actions">
            <button type="button" className="fmv-project-action fmv-project-dark" onClick={openCreate}><Plus size={18} aria-hidden="true" />新建项目</button>
            <Link href={importPath} className="fmv-project-action"><Upload size={17} aria-hidden="true" />导入与恢复完整项目</Link>
          </div>
        </section>}
        {featured && <section className="fmv-project-recent" aria-labelledby="fmv-project-recent-title">
          <h3 id="fmv-project-recent-title">最近编辑</h3>
          <article className="fmv-project-feature" data-testid="project-feature">
            <ProjectPoster source={coverSource} size="feature" />
            <div className="fmv-project-feature-copy">
              <h4>{featured.title}</h4>
              <p>版本 {featured.revision} · 当前保存版本</p>
              <div className="fmv-project-actions">
                <Link href={editorPath(featured.id)} className="fmv-project-action">继续创作<ArrowRight size={16} aria-hidden="true" /></Link>
                <Link href={playerPath(featured.id)} className="fmv-project-action fmv-project-outline"><Play size={16} aria-hidden="true" />网页试玩</Link>
              </div>
            </div>
          </article>
        </section>}
        {projects && projects.length > 0 && <section className="fmv-project-list" aria-labelledby="fmv-project-list-title">
          <h3 id="fmv-project-list-title">项目列表</h3>
          {projects.map((project) => <article className="fmv-project-row" data-testid="project-item" key={project.id}>
            <ProjectPoster source={project.id === featuredId ? coverSource : undefined} size="row" />
            <div className="fmv-project-row-info">
              <h4><Link href={editorPath(project.id)}>{project.title}</Link></h4>
              <p className="fmv-project-note">版本 {project.revision}</p>
            </div>
            <div className="fmv-project-actions">
              <Link href={playerPath(project.id)} className="fmv-project-action" data-testid="project-play"><Play size={16} aria-hidden="true" />网页试玩</Link>
              <Link href={editorPath(project.id)} className="fmv-project-action">打开项目<ArrowRight size={16} aria-hidden="true" /></Link>
            </div>
          </article>)}
        </section>}
      </main>
    </div>
    <Dialog open={createOpen} onOpenChange={(open) => { if (!busy) setCreateOpen(open); }}>
      <DialogContent className="fmv-project-create-dialog" onCloseAutoFocus={(event) => {
        event.preventDefault();
        createTrigger.current?.focus();
      }}>
        <DialogTitle>新建项目</DialogTitle>
        <form onSubmit={(event) => { void create(event); }}>
          <label htmlFor="fmv-project-name">项目名称</label>
          <input id="fmv-project-name" data-testid="project-title" placeholder="例如：午夜列车" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={120} disabled={busy} />
          <DialogDescription>创建后可直接进入剧情编排。</DialogDescription>
          {createError && <p role="alert" className="fmv-project-error">{createError}</p>}
          <div className="fmv-project-actions">
            <button type="button" className="fmv-project-action" onClick={() => setCreateOpen(false)} disabled={busy}>取消</button>
            <button type="submit" className="fmv-project-action fmv-project-dark" data-testid="project-create" disabled={busy}><Plus size={17} aria-hidden="true" />{busy ? '正在创建…' : '创建项目'}</button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  </WorkspaceShell>;
}
