'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';

import { authenticate } from '@/app/_utils/cloudApi';
import { errorText } from './WorkspaceShell';

export default function AccountPage() {
  const router = useRouter();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    await authenticate(mode, email, password).then(() => {
      setPassword(''); router.replace('/zh-CN/projects');
    }).catch((problem: unknown) => setError(errorText(problem))).finally(() => setBusy(false));
  }
  return <main className="fmv-auth">
    <section className="fmv-auth-card">
      <img src="/logo.png" width="52" height="52" alt="OpenFMV" />
      <p className="fmv-eyebrow">互动游戏创作工作台</p>
      <h1>{mode === 'register' ? '创建你的账号' : '登录 OpenFMV'}</h1>
      <p className="fmv-muted">让每一段故事，拥有自己的选择。</p>
      <form onSubmit={(event) => { void submit(event); }}>
        <label>邮箱<input data-testid="auth-email" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
        <label>密码<input data-testid="auth-password" type="password" minLength={10} maxLength={256} autoComplete={mode === 'register' ? 'new-password' : 'current-password'} value={password} onChange={(event) => setPassword(event.target.value)} required /><span className="fmv-muted">至少10个字符，最多256个字符。</span></label>
        {error && <p role="alert" className="fmv-error">{error}</p>}
        <button className="fmv-primary" data-testid="auth-submit" disabled={busy}>{busy ? '正在提交…' : mode === 'register' ? '注册并进入工作台' : '登录'}</button>
      </form>
      <button className="fmv-text-button" data-testid="auth-mode-register" onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); }}>{mode === 'login' ? '创建新账号' : '使用已有账号登录'}</button>
    </section>
  </main>;
}
