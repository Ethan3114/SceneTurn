'use client';

import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import Link from 'next/link';
import { useLocale } from 'next-intl';
import { useSearchParams } from 'next/navigation';
import { ArrowLeft, ChevronDown } from 'lucide-react';

import ModelPickerDialog from '@/app/_features/material-workspace/ModelPickerDialog';
import { operationId, projectList, type ProjectSummary } from '@/app/_utils/cloudApi';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import { getProviderCatalog, modelKinds, modelKindLabels, saveModelDefaults, saveProviderConnection, type ModelDefaults,
  type ModelKind, type ModelProfile, type ProviderCatalog, type ProviderConnection, type ProviderPrice } from '@/app/_utils/providerApi';
import { errorText, useWorkspaceUser, WorkspaceShell } from './WorkspaceShell';

const providerTitles: Record<string, string> = {
  deepseek: 'DeepSeek', 'aliyun-qwen': '阿里云百炼 · 千问', sensetime: '商汤', 'volcengine-ark': '火山方舟', 'aliyun-wan': '阿里云百炼 · Wan',
};

export default function ModelsSettingsPage() {
  const locale = useLocale();
  const { user, error: userError } = useWorkspaceUser();
  const searchParams = useSearchParams();
  const [projectId, setProjectId] = useState(searchParams.get('projectId') ?? '');
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [preferred, setPreferred] = useState<ModelDefaults | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [pickerKind, setPickerKind] = useState<ModelKind | null>(null);
  const [pickerPending, setPickerPending] = useState(false);
  const [pickerError, setPickerError] = useState('');
  const [pickerRefresh, setPickerRefresh] = useState(0);
  const pending = useRef<{ identity: string; operation: string } | null>(null);

  useEffect(() => {
    if (!user) return;
    let active = true;
    void projectList().then((value) => { if (active) setProjects(value.projects); }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    return () => { active = false; };
  }, [user]);

  useEffect(() => {
    if (!user) return;
    let active = true;
    void getProviderCatalog(projectId || undefined).then((value) => {
      if (active) { setCatalog(value); setPreferred(value.defaults); }
    }).catch((problem: unknown) => { if (active) setError(errorText(problem)); });
    return () => { active = false; };
  }, [projectId, refresh, user]);

  useEffect(() => {
    if (!pickerKind || !projectId) return;
    let active = true;
    void getProviderCatalog(projectId).then((value) => {
      if (!active) return;
      setCatalog((current) => current && { ...current, models: value.models });
      setPickerPending(false);
    }).catch((problem: unknown) => {
      if (!active) return;
      setPickerError(errorText(problem));
      setPickerPending(false);
    });
    return () => { active = false; };
  }, [projectId, pickerKind, pickerRefresh]);

  useEffect(() => {
    if (!pickerKind) return;
    function refreshOpenPicker() {
      setPickerError('');
      setPickerPending(true);
      setPickerRefresh((current) => current + 1);
    }
    window.addEventListener('focus', refreshOpenPicker);
    return () => window.removeEventListener('focus', refreshOpenPicker);
  }, [pickerKind]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!projectId || !preferred || catalog?.revision == null || saving) return;
    setSaving(true); setError(''); setNotice('');
    const revision = catalog.revision;
    const identity = JSON.stringify({ projectId, preferred, revision });
    const request = pending.current?.identity === identity ? pending.current : { identity, operation: operationId() };
    pending.current = request;
    try {
      const result = await saveModelDefaults(projectId, preferred, revision, request.operation);
      setCatalog((current) => current && { ...current, ...result });
      setPreferred(result.defaults);
      setNotice('项目默认模型已保存');
      pending.current = null;
    } catch (problem) {
      setError(errorText(problem));
    } finally {
      setSaving(false);
    }
  }

  if (!user) return <main className="fmv-loading">{userError || '正在读取账号…'}</main>;
  const editorPath = getLocalizedPath(locale, '/editor');
  const projectEditor = projectId ? `${editorPath}?id=${encodeURIComponent(projectId)}` : null;
  return <WorkspaceShell user={user} title="模型设置" monochrome>
    <main className="fmv-models">
      <section className="fmv-panel fmv-model-preferences">
        <h2>项目默认模型</h2>
        <p className="fmv-muted">默认模型用于新建生成任务，已提交的任务保留原配置。</p>
        <form onSubmit={(event) => { void save(event); }}>
          <label>项目<select value={projectId} disabled={saving} onChange={(event) => { setProjectId(event.target.value); setCatalog(null); setError(''); setNotice(''); }}><option value="">选择项目</option>{projects.map(({ id, title }) => <option key={id} value={id}>{title}</option>)}</select></label>
          {preferred && catalog && projectId && <div className="fmv-model-grid">{modelKinds.map((kind) => {
            const current = catalog.models.find((model) => model.id === preferred[kind]);
            return <div className="fmv-model-default" key={kind}>
              <span>{modelKindLabels[kind]}模型</span>
              <ModelPickerDialog kind={kind} open={pickerKind === kind} catalog={catalog} selection={preferred}
                loading={pickerPending} error={pickerError} onRetry={() => {
                  setPickerError(''); setPickerPending(true); setPickerRefresh((value) => value + 1);
                }}
                onOpenChange={(open) => {
                  setPickerKind(open ? kind : null);
                  setPickerError('');
                  setPickerPending(open);
                }}
                onApply={(choice) => {
                  setPreferred({ ...preferred, ...choice }); setNotice('');
                }}
                connectHref={projectEditor ? `${projectEditor}&connectCodex=1` : undefined}
                trigger={<button type="button" disabled={saving} aria-label={`${modelKindLabels[kind]}模型 ${current?.label ?? preferred[kind]}`}>
                  <span>{current?.label ?? preferred[kind]}</span>
                  <ChevronDown size={17} aria-hidden="true" />
                </button>}
              />
            </div>;
          })}</div>}
          {projectId && <button type="submit" className="fmv-primary" disabled={!catalog || saving}>保存默认模型</button>}
        </form>
        {notice && <p role="status" className="fmv-success">{notice}</p>}
        {error && <p role="alert" className="fmv-error">{error}</p>}
      </section>
      {catalog ? <>
        <section className="fmv-panel fmv-model-directory" aria-label="模型目录"><h2>模型目录</h2>
          <div className="fmv-model-grid">{modelKinds.map((kind) => <section key={kind}><h3>{modelKindLabels[kind]}</h3><ul>
            {catalog.models.filter((model) => model.kind === kind).map((model) => <li key={model.id}>
              <strong>{model.label}</strong><p className="fmv-muted">{model.available ? '可选择' : model.unavailableReason || '尚未准备'}</p>
              {model.kind === 'video' && <p className="fmv-muted">{model.capabilities.lastFrame ? '支持首尾帧' : '支持首帧'} · {model.capabilities.resolutions?.join(' / ')}</p>}
            </li>)}
          </ul></section>)}</div>
        </section>
        <section className="fmv-panel fmv-model-services"><h2>服务连接</h2>
          <div className="fmv-model-connections">{catalog.connections.map((connection) => <ProviderForm key={`${connection.providerId}:${connection.revision}`} connection={connection} models={catalog.models.filter((model) => model.providerId === connection.providerId)} onSave={() => setRefresh((value) => value + 1)} />)}</div>
          <p className="fmv-muted">在项目的「问问 AI」连接支持图片生成的个人 Codex，连接后可在图片模型中选择。</p>
          <Link className="fmv-settings-link" href={projectEditor ?? getLocalizedPath(locale, '/projects')}>
            <ArrowLeft size={16} aria-hidden="true" />
            {projectEditor ? '回到剧情画布' : '返回项目'}
          </Link>
        </section>
      </> : !error && <p role="status" className="fmv-muted">正在读取模型目录…</p>}
    </main>
  </WorkspaceShell>;
}

function ProviderForm({ connection, models, onSave }: { connection: ProviderConnection; models: ModelProfile[]; onSave: () => void }) {
  const locale = useLocale();
  const { providerId } = connection;
  const [secret, setSecret] = useState('');
  const [keys, setKeys] = useState(['', '', '']);
  const [workspace, setWorkspace] = useState(connection.workspaceId ?? '');
  const [entitled, setEntitled] = useState(connection.entitledModelIds);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const legacyPath = providerId === 'deepseek' ? '/settings/text' : providerId === 'aliyun-wan' ? '/settings/wan' : null;

  async function configure(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError('');
    try {
      if (providerId === 'sensetime' && keys.some((key) => !key.trim())) throw new Error('请填写全部三个商汤账号的凭据');
      await saveProviderConnection(providerId, { expectedRevision: connection.revision, apiKey: secret.trim() || undefined,
        apiKeys: providerId === 'sensetime' ? keys.map((key) => key.trim()) : undefined,
        workspaceId: workspace.trim() || undefined, entitledModelIds: providerId === 'volcengine-ark' ? entitled : undefined });
      setSecret(''); setKeys(['', '', '']);
      onSave();
    } catch (problem) {
      setError(errorText(problem));
    } finally {
      setBusy(false);
    }
  }

  return <details className="fmv-model-connection">
    <summary>{providerTitles[providerId] ?? providerId} · {connection.configured ? '已配置' : '未配置'}</summary>
    {legacyPath ? <Link className="fmv-settings-link" href={getLocalizedPath(locale, legacyPath)}>打开服务设置</Link> : connection.canManage ? <>
      <form onSubmit={(event) => { void configure(event); }}>
        {providerId === 'sensetime' ? keys.map((key, index) => <label key={index}>商汤 API Key {index + 1}<input type="password" required autoComplete="new-password" value={key} onChange={(event) => setKeys((current) => current.map((item, slot) => slot === index ? event.target.value : item))} /></label>) :
          <label>API Key<input type="password" autoComplete="new-password" required={!connection.configured} value={secret} onChange={(event) => setSecret(event.target.value)} placeholder={connection.configured ? '留空保留原凭据' : '填写当前账号的供应商凭据'} /></label>}
        {providerId === 'aliyun-qwen' && <label>北京业务空间 ID<input required value={workspace} onChange={(event) => setWorkspace(event.target.value)} /></label>}
        {models.some((model) => model.requiresEntitlement) && <fieldset><legend>账号已开通的型号</legend>{models.filter((model) => model.requiresEntitlement).map((model) => <label className="fmv-model-check" key={model.id}>
          <input type="checkbox" checked={entitled.includes(model.id)} onChange={(event) => setEntitled((values) => event.target.checked ? [...values, model.id] : values.filter((id) => id !== model.id))} />{model.label}
        </label>)}</fieldset>}
        {error && <p role="alert" className="fmv-error">{error}</p>}
        <button type="submit" disabled={busy}>保存连接</button>
      </form>
      {connection.configured && providerId !== 'sensetime' && <ModelPriceForm connection={connection} models={models} onSave={onSave} />}
    </> : <p className="fmv-muted">由平台管理员管理。</p>}
  </details>;
}

function ModelPriceForm({ connection, models, onSave }: { connection: ProviderConnection; models: ModelProfile[]; onSave: () => void }) {
  const [id, setId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const model = models.find((item) => item.id === id);
  const previous = connection.prices[id];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!model || busy) return;
    const form = new FormData(event.currentTarget);
    const amount = (key: string, scale: number) => Math.round(Number(form.get(key)) * scale);
    const price: ProviderPrice = { version: String(form.get('version')).trim(), verified: true, currency: 'CNY' };
    if (model.kind === 'text') {
      price.cacheHitMicrosPerMillion = amount('hit', 1e6);
      price.cacheMissMicrosPerMillion = amount('miss', 1e6);
      price.outputMicrosPerMillion = amount('output', 1e6);
    } else if (model.kind === 'image') {
      price.microsPerImage = amount('image', 1e6);
    } else {
      price.outputMicrosPerMillion = amount('output', 1e6);
      price.centsPerSecond = Object.fromEntries((model.capabilities.resolutions ?? []).filter((resolution) => form.get(resolution) !== '').map((resolution) => [resolution, amount(resolution, 100)]));
      if (Object.keys(price.centsPerSecond).length === 0) { setError('请填写至少一种分辨率的实际价格'); return; }
    }
    setBusy(true); setError('');
    try {
      await saveProviderConnection(connection.providerId, { expectedRevision: connection.revision, prices: { ...connection.prices, [id]: price } });
      onSave();
    } catch (problem) {
      setError(errorText(problem));
    } finally {
      setBusy(false);
    }
  }

  const yuan = (value: number | null | undefined, scale: number) => value == null ? '' : value / scale;
  return <form key={id} onSubmit={(event) => { void submit(event); }}>
    <label>已核对价表的模型<select value={id} onChange={(event) => { setId(event.target.value); setError(''); }}><option value="">选择模型</option>{models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
    {model && <>
      <label>价格依据或版本<input name="version" required maxLength={100} defaultValue={previous?.version} /></label>
      {model.kind === 'text' && <>
        <label>缓存命中输入（元 / 百万 Token）<input type="number" name="hit" min="0" step="0.000001" required defaultValue={yuan(previous?.cacheHitMicrosPerMillion, 1e6)} /></label>
        <label>普通输入（元 / 百万 Token）<input type="number" name="miss" min="0" step="0.000001" required defaultValue={yuan(previous?.cacheMissMicrosPerMillion, 1e6)} /></label>
        <label>输出（元 / 百万 Token）<input type="number" name="output" min="0" step="0.000001" required defaultValue={yuan(previous?.outputMicrosPerMillion, 1e6)} /></label>
      </>}
      {model.kind === 'image' && <label>每张图片价格（元）<input type="number" name="image" min="0.000001" step="0.000001" required defaultValue={yuan(previous?.microsPerImage, 1e6)} /></label>}
      {model.kind === 'video' && <>
        <label>输出（元 / 百万 Token）<input type="number" name="output" min="0.000001" step="0.000001" required defaultValue={yuan(previous?.outputMicrosPerMillion, 1e6)} /></label>
        {(model.capabilities.resolutions ?? []).map((resolution) => <label key={resolution}>{resolution} 每秒费用上限（元）<input type="number" name={resolution} min="0.01" step="0.01" defaultValue={yuan(previous?.centsPerSecond?.[resolution], 100)} /></label>)}
      </>}
      <label className="fmv-model-check"><input type="checkbox" required />我已核对本账号的实际计费规则</label>
      {error && <p role="alert" className="fmv-error">{error}</p>}
      <button type="submit" disabled={busy}>保存价表</button>
    </>}
  </form>;
}
