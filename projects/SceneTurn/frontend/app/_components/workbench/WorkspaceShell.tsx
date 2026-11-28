'use client';

import React, { useEffect, useId, useState } from 'react';
import Link from 'next/link';
import { useLocale } from 'next-intl';
import { useRouter } from 'next/navigation';
import { LogOut, Settings2 } from 'lucide-react';

import { ApiError, currentUser, logout, type CloudUser } from '@/app/_utils/cloudApi';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import SceneTurnMark from './SceneTurnMark';
import './workbench.css';
import './sceneturn-workspace.css';

export function useWorkspaceUser() {
  const router = useRouter();
  const locale = useLocale();
  const [user, setUser] = useState<CloudUser | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    currentUser().then((value) => { if (active) setUser(value); }).catch((problem: unknown) => {
      if (!active) return;
      if (problem instanceof ApiError && problem.status === 401) router.replace(getLocalizedPath(locale, '/account'));
      else setError(problem instanceof Error ? problem.message : '无法读取账号。');
    });
    return () => { active = false; };
  }, [locale, router]);
  return { user, error };
}

export function WorkspaceShell({ user, title, children, canvas = false, monochrome = false, projectLibrary = false }: { user: CloudUser; title: string; children: React.ReactNode; canvas?: boolean; monochrome?: boolean; projectLibrary?: boolean }) {
  const router = useRouter();
  const locale = useLocale();
  const [error, setError] = useState('');
  const accountPopover = useId();
  function endSession() {
    void logout().then(() => {
      useProjectSessionStore.getState().resetSession();
      router.replace(getLocalizedPath(locale, '/account'));
    }).catch((problem: unknown) => setError(errorText(problem)));
  }

  if (projectLibrary) return <div className="fmv-workspace fmv-project-workspace">
    <header className="fmv-workspace-header fmv-project-topbar">
      <div className="fmv-project-brand">
        <Link href={getLocalizedPath(locale, '/')} className="fmv-brand"><SceneTurnMark monochrome />SceneTurn</Link>
        <h1>{title}</h1>
      </div>
      <nav aria-label="工作台导航">
        <Link href={getLocalizedPath(locale, '/settings/models')} className="fmv-project-action"><Settings2 size={16} aria-hidden="true" />模型设置</Link>
        <button type="button" className="fmv-project-account" aria-label="账户" popoverTarget={accountPopover}>{user.email.slice(0, 2).toUpperCase()}</button>
      </nav>
    </header>
    <section className="fmv-project-account-popover" id={accountPopover} popover="auto" aria-label="账户">
      <header><h2>账户</h2><button className="fmv-project-action" type="button" popoverTarget={accountPopover} popoverTargetAction="hide">关闭</button></header>
      <p>{user.email}</p>
      <Link href={getLocalizedPath(locale, '/settings/models')} className="fmv-project-action"><Settings2 size={16} aria-hidden="true" />模型设置</Link>
      <button type="button" className="fmv-project-action" data-testid="logout" onClick={endSession}><LogOut size={16} aria-hidden="true" />退出登录</button>
    </section>
    {error && <p role="alert" className="fmv-error">{error}</p>}
    {children}
  </div>;

  return <div className={canvas ? 'fmv-creation-workspace' : monochrome ? 'fmv-workspace fmv-workspace-monochrome' : 'fmv-workspace'}>
    <header className="fmv-workspace-header">
      <div><Link href={getLocalizedPath(locale, '/')} className="fmv-brand"><SceneTurnMark monochrome={monochrome} />SceneTurn</Link><h1>{title}</h1></div>
      <nav aria-label="工作台导航">
        <Link href={getLocalizedPath(locale, '/projects')}>项目</Link>
        <Link href={getLocalizedPath(locale, '/settings/models')}>模型设置</Link>
        <Link href={getLocalizedPath(locale, '/settings/text')} data-testid="text-provider-settings">文字设置</Link>
        <Link href={getLocalizedPath(locale, '/settings/wan')} data-testid="provider-settings">视频设置</Link>
        <span className="fmv-account">{user.email}</span>
        <button type="button" data-testid="logout" onClick={endSession}>退出登录</button>
      </nav>
    </header>
    {error && <p role="alert" className="fmv-error">{error}</p>}
    {children}
  </div>;
}

export const errorText = (problem: unknown) => problem instanceof Error ? problem.message : '操作失败，请检查当前状态。';
export const money = (cents: number) => `¥${(cents / 100).toFixed(2)}`;
