'use client';

import React, { useEffect, useState } from 'react';

import { getWanSettings, saveWanSettings, type WanSettings } from '@/app/_utils/cloudApi';
import { WorkspaceShell, useWorkspaceUser, errorText } from './WorkspaceShell';

export default function WanSettingsPage() {
  const { user, error: userError } = useWorkspaceUser();
  const [settings, setSettings] = useState<WanSettings | null>(null);
  const [workspaceId, setWorkspaceId] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!user) return;
    getWanSettings().then((value) => { setSettings(value); setWorkspaceId(value.workspaceId || ''); }).catch((problem: unknown) => setError(errorText(problem)));
  }, [user]);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setSaved(false); setError('');
    await saveWanSettings({ workspaceId, apiKey, region: 'cn-beijing', model: 'wan3.0-video' }).then((value) => {
      setSettings(value); setApiKey(''); setSaved(true);
    }).catch((problem: unknown) => setError(errorText(problem))).finally(() => setBusy(false));
  }
  if (!user) return <main className="fmv-loading">{userError || '正在读取账号…'}</main>;
  return <WorkspaceShell user={user} title="视频生成设置"><main className="fmv-settings"><section className="fmv-panel"><p className="fmv-eyebrow">阿里云百炼 · 北京</p><h2>连接 Wan 视频服务</h2><p className="fmv-muted">生成前逐次展示提示词、模型、参数和最高费用，由你确认。</p><p role="status">{settings?.configured ? '已保存服务配置' : '尚未配置服务'}</p><form onSubmit={(event) => { void submit(event); }}>
    <label>业务空间 ID<input data-testid="wan-workspace" autoComplete="off" value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)} required /></label>
    <label>API Key<input data-testid="wan-key" type="password" autoComplete="new-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} required /></label>
    <p className="fmv-muted">凭据加密保存在服务端，保存后输入框清空。每次生成最高 ¥10，本次模型验证累计最高 ¥300。</p>
    {error && <p className="fmv-error" role="alert">{error}</p>}{saved && <p className="fmv-success" role="status">配置已安全保存。</p>}
    <button className="fmv-primary" disabled={busy}>{busy ? '正在保存…' : '保存生成配置'}</button>
  </form></section></main></WorkspaceShell>;
}
