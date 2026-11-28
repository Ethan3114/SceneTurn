'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, Eye, EyeOff } from 'lucide-react';
import { useLocale } from 'next-intl';
import { useRouter } from 'next/navigation';

import { authenticate } from '@/app/_utils/cloudApi';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import { errorText } from './WorkspaceShell';
import SceneTurnMark from './SceneTurnMark';

export default function AccountPage() {
  const router = useRouter();
  const locale = useLocale();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const registering = mode === 'register';
  const homePath = getLocalizedPath(locale, '/');

  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    await authenticate(mode, email, password).then(() => {
      setPassword(''); router.replace(getLocalizedPath(locale, '/projects'));
    }).catch((problem: unknown) => setError(errorText(problem))).finally(() => setBusy(false));
  }

  function switchMode() {
    setMode(registering ? 'login' : 'register');
    setPasswordVisible(false);
    setError('');
  }

  return <main className="fmv-auth fmv-scene-auth">
    <section className="fmv-auth-visual" data-mode={mode} aria-label="潮汐尽头">
      <Link href={homePath} className="fmv-brand"><SceneTurnMark monochrome />SceneTurn</Link>
      <div className="fmv-auth-caption">
        <p>互动电影</p>
        <strong>潮汐尽头</strong>
        <p>一束信标，两条回家的路。</p>
      </div>
    </section>
    <section className="fmv-auth-panel" aria-labelledby="scene-auth-title">
      <Link href={homePath} className="fmv-auth-home"><ArrowLeft size={16} aria-hidden="true" />返回首页</Link>
      <div className="fmv-auth-card">
        <h1 id="scene-auth-title">{registering ? '创建账号' : '欢迎回来'}</h1>
        <p className="fmv-auth-intro">{registering ? '创建账号后进入你的创作工作台。' : '登录后打开项目、素材和制作进度。'}</p>
        <form onSubmit={(event) => { void submit(event); }}>
          <label>
            邮箱
            <input data-testid="auth-email" type="email" autoComplete="email" placeholder="输入邮箱地址" value={email} onChange={(event) => setEmail(event.target.value)} required />
          </label>
          <div className="fmv-auth-field">
            <label htmlFor="scene-auth-password">密码</label>
            <div className="fmv-auth-password">
              <input id="scene-auth-password" data-testid="auth-password" type={passwordVisible ? 'text' : 'password'} minLength={10} maxLength={256} autoComplete={registering ? 'new-password' : 'current-password'} placeholder="输入密码" aria-describedby={registering ? 'scene-auth-password-help' : undefined} value={password} onChange={(event) => setPassword(event.target.value)} required />
              <button className="fmv-auth-password-toggle" type="button" aria-label={passwordVisible ? '隐藏密码' : '显示密码'} aria-controls="scene-auth-password" onClick={() => setPasswordVisible((visible) => !visible)}>
                {passwordVisible ? <EyeOff size={18} aria-hidden="true" /> : <Eye size={18} aria-hidden="true" />}
              </button>
            </div>
            {registering && <span id="scene-auth-password-help" className="fmv-auth-help">为账号设置至少 10 个字符的密码。</span>}
          </div>
          {error && <p role="alert" className="fmv-error">{error}</p>}
          <button type="submit" className="fmv-primary" data-testid="auth-submit" disabled={busy}>{busy ? '正在提交…' : registering ? '注册并进入工作台' : '登录'}</button>
        </form>
        <div className="fmv-auth-switch">
          <span>{registering ? '已有账号？' : '没有账号？'}</span>
          <button type="button" data-testid="auth-mode-register" onClick={switchMode} disabled={busy}>{registering ? '去登录' : '创建账号'}</button>
        </div>
      </div>
      <nav className="fmv-auth-legal" aria-label="法律信息">
        <Link href={getLocalizedPath(locale, '/privacy')}>隐私政策</Link>
        <Link href={getLocalizedPath(locale, '/terms')}>使用条款</Link>
      </nav>
    </section>
  </main>;
}
