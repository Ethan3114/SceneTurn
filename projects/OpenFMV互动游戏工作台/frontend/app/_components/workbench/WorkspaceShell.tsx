'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

import { ApiError, currentUser, logout, type CloudUser } from '@/app/_utils/cloudApi';
import { useProjectSessionStore } from '@/app/_features/project-session/store';
import './workbench.css';

export function useWorkspaceUser() {
  const router = useRouter();
  const [user, setUser] = useState<CloudUser | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    currentUser().then((value) => { if (active) setUser(value); }).catch((problem: unknown) => {
      if (!active) return;
      if (problem instanceof ApiError && problem.status === 401) router.replace('/zh-CN/account');
      else setError(problem instanceof Error ? problem.message : '无法读取账号。');
    });
    return () => { active = false; };
  }, [router]);
  return { user, error };
}

export function WorkspaceShell({ user, title, children, canvas = false }: { user: CloudUser; title: string; children: React.ReactNode; canvas?: boolean }) {
  const router = useRouter();
  const [error, setError] = useState('');
  return <div className={canvas ? 'fmv-creation-workspace' : 'fmv-workspace'}>
    <header className="fmv-workspace-header">
      <div><Link href="/zh-CN/projects" className="fmv-brand">OpenFMV</Link><h1>{title}</h1></div>
      <nav aria-label="工作台导航"><Link href="/zh-CN/projects">项目</Link><Link href="/zh-CN/settings/text" data-testid="text-provider-settings">文字设置</Link><Link href="/zh-CN/settings/wan" data-testid="provider-settings">视频设置</Link><span className="fmv-account">{user.email}</span><button data-testid="logout" onClick={() => { logout().then(() => { useProjectSessionStore.getState().resetSession(); router.replace('/zh-CN/account'); }).catch((problem: Error) => setError(problem.message)); }}>退出登录</button></nav>
    </header>
    {error && <p role="alert" className="fmv-error">{error}</p>}
    {children}
  </div>;
}

export const errorText = (problem: unknown) => problem instanceof Error ? problem.message : '操作失败，请检查当前状态。';
export const money = (cents: number) => `¥${(cents / 100).toFixed(2)}`;
