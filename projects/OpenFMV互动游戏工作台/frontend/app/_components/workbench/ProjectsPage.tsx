'use client';

import React, { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Film, Play, Plus } from 'lucide-react';

import { api, createCloudProject, projectList, type ProjectSummary } from '@/app/_utils/cloudApi';
import { WorkspaceShell, useWorkspaceUser, errorText } from './WorkspaceShell';
import ProjectTransfers from '@/app/_features/project-transfers/ProjectTransfers';

export default function ProjectsPage() {
  const { user, error: userError } = useWorkspaceUser();
  const router = useRouter();
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [title, setTitle] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [payment, setPayment] = useState('正在检查支付配置…');
  const refreshProjects = useCallback(() => { void projectList().then((value) => setProjects(value.projects)).catch((problem: unknown) => setError(errorText(problem))); }, []);
  useEffect(() => {
    if (!user) return;
    projectList().then((value) => setProjects(value.projects)).catch((problem: unknown) => setError(errorText(problem)));
    api<{ configured: boolean; provider: string | null }>('/payments/capabilities').then((value) => setPayment(value.configured ? '支付已配置' : '支付接口未配置')).catch((problem: unknown) => setError(errorText(problem)));
  }, [user]);
  async function create(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    await createCloudProject(title).then(({ project }) => router.push(`/zh-CN/editor?id=${encodeURIComponent(project.id)}`)).catch((problem: unknown) => setError(errorText(problem))).finally(() => setBusy(false));
  }
  if (!user) return <main className="fmv-loading">{userError || '正在读取账号…'}</main>;
  return <WorkspaceShell user={user} title="我的项目">
    <main className="fmv-projects">
      <section className="fmv-project-intro"><p className="fmv-eyebrow">创作空间</p><h2>从一个场景开始</h2><p className="fmv-muted">组织故事分支，在剧情画布制作素材，编排互动时间线。</p></section>
      <form className="fmv-create-project" onSubmit={(event) => { void create(event); }}><label>项目名称<input data-testid="project-title" placeholder="例如：午夜列车" value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={120} /></label><button className="fmv-primary" data-testid="project-create" disabled={busy}><Plus size={18} />{busy ? '正在创建…' : '创建项目'}</button></form>
      {error && <p role="alert" className="fmv-error">{error}</p>}
      <section className="fmv-project-grid" aria-label="项目列表">{projects.map((project) => <article className="fmv-project-card" data-testid="project-item" key={project.id}>
        <Link href={`/zh-CN/editor?id=${encodeURIComponent(project.id)}`}><div className="fmv-project-cover"><Film size={42} /></div><h3>{project.title}</h3></Link>
        <p className="fmv-muted">版本 {project.revision} · {new Date(project.updatedAt).toLocaleString('zh-CN')}</p>
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm"><Link href={`/zh-CN/editor?id=${encodeURIComponent(project.id)}`}>进入创作 →</Link><Link data-testid="project-play" className="inline-flex items-center gap-2 rounded-lg border border-white/20 px-3 py-2" href={`/zh-CN/play/${encodeURIComponent(project.id)}`}><Play size={14} />在网页游玩</Link></div>
      </article>)}</section>
      {projects.length === 0 && <p className="fmv-empty">创建项目后，故事和素材会出现在这里。</p>}
      <p className="fmv-muted" data-testid="payment-status">{payment}</p>
      <details className="fmv-compatibility"><summary>导入与恢复完整项目</summary><ProjectTransfers onImported={refreshProjects} /><Link href="/zh-CN/assets">打开素材库</Link></details>
    </main>
  </WorkspaceShell>;
}
