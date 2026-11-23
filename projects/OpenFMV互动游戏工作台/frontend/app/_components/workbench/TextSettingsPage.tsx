'use client';

import React, { useEffect, useState } from 'react';

import { getTextSettings, saveTextSettings, type TextSettings } from '@/app/_utils/cloudApi';
import { WorkspaceShell, useWorkspaceUser, errorText } from './WorkspaceShell';

export default function TextSettingsPage() {
  const { user, error: userError } = useWorkspaceUser();
  const [settings, setSettings] = useState<TextSettings | null>(null);
  const [apiKey, setApiKey] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!user) return;
    let active = true;
    getTextSettings().then((value) => { if (active) setSettings(value); }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    return () => { active = false; };
  }, [user]);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setSaved(false); setError('');
    await saveTextSettings({ apiKey, provider: 'deepseek', model: 'deepseek-flash' }).then((value) => { setSettings(value); setApiKey(''); setSaved(true); }).catch((problem: unknown) => setError(errorText(problem))).finally(() => setBusy(false));
  }
  if (!user) return <main className="fmv-loading">{userError || '正在读取账号…'}</main>;
  return <WorkspaceShell user={user} title="文字生成设置" monochrome><main className="fmv-settings fmv-service-settings"><section className="fmv-panel"><p className="fmv-eyebrow">DeepSeek API</p><h2>文字生成服务</h2><p className="fmv-muted">使用 deepseek-flash 生成剧本摘要、角色画像、道具提示词、场景说明、分镜情节与单镜提示词。</p><p role="status" data-testid="text-settings-status">{settings?.configured ? '已保存文字服务配置' : '尚未配置文字服务'}</p>
    <p className="fmv-muted">按任务记录真实 Token 用量，支持核对执行记录。</p>
    {settings?.canManage ? <form onSubmit={(event) => { void submit(event); }}>
    <label>模型<input data-testid="text-model" value="deepseek-flash" readOnly /></label>
    <label>API Key<input data-testid="text-api-key" type="password" autoComplete="new-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} required /></label>
    <p className="fmv-muted">平台凭据加密保存在服务端，保存后输入框清空。文字生成调用真实 API，在任务中展示输入、输出与缓存 Token 用量。</p>
    {error && <p className="fmv-error" role="alert">{error}</p>}{saved && <p className="fmv-success" role="status">文字配置已安全保存。</p>}
    <button className="fmv-primary" data-testid="text-settings-save" disabled={busy}>{busy ? '正在保存…' : '保存文字生成配置'}</button>
  </form> : <p data-testid="text-admin-only">文字服务由平台管理员配置。</p>}{error && !settings?.canManage && <p className="fmv-error" role="alert">{error}</p>}</section></main></WorkspaceShell>;
}
