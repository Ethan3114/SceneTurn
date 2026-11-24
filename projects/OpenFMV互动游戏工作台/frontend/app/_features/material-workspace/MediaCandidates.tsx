'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTaskPolling } from '@/app/_hooks/useTaskPolling';
import { assetContentUrl, operationId } from '@/app/_utils/cloudApi';
import { applyMediaAdoption, checkMediaAdoption, confirmMediaCandidate, createMediaSequence, getMediaCandidates, keepMediaVersion, quoteSequenceSegment, type MediaCandidate, type MediaCandidates as CandidateData, type MediaSequence, type SequenceSegmentInput } from '@/app/_utils/mediaCandidateApi';
import { syncProjectSession } from '@/app/_utils/syncProjectSession';
import { useMaterialWorkspace } from './store';
import { taskLabels } from '@/app/_features/creation-canvas/GenerationTaskCard';

const activeCandidateStatuses = new Set(['queued', 'submitting', 'processing', 'downloading']);

function SequenceBuilder({ data, conversationId, busy, perform }: { data: CandidateData; conversationId: string; busy: boolean; perform: (action: () => Promise<void>) => Promise<void> }) {
  const [frames, setFrames] = useState<string[]>(['', '']);
  const [segments, setSegments] = useState<SequenceSegmentInput[]>([{ title: '片段1', prompt: '', duration: 2, resolution: '720P' }]);
  const operationRef = useRef<{ payload: string; id: string } | null>(null);
  function changeFrames(next: string[]) {
    setFrames(next);
    setSegments((previous) => Array.from({ length: next.length - 1 }, (_, index) => previous[index] || { title: `片段${index + 1}`, prompt: '', duration: 2, resolution: '720P' }));
  }
  function changeSegment(index: number, update: Partial<SequenceSegmentInput>) { setSegments((previous) => previous.map((item, position) => position === index ? { ...item, ...update } : item)); }
  async function save() {
    const payload = JSON.stringify({ frames, segments, revision: data.revision });
    const request = operationRef.current?.payload === payload ? operationRef.current : { payload, id: operationId() };
    operationRef.current = request;
    await createMediaSequence(data.projectId, data.sceneId, conversationId, '连续首尾帧视频', frames, segments, data.revision, request.id);
    operationRef.current = null;
    setFrames(['', '']); setSegments([{ title: '片段1', prompt: '', duration: 2, resolution: '720P' }]);
  }
  return <details className="material-sequence-builder"><summary>手动选择连续首尾图片</summary><p>按时间顺序选择图片。{segments.length}个视频片段共用{frames.length}张边界图片。</p>
    {frames.map((identifier, index) => <label key={index}><span>边界图片{index + 1}</span><select aria-label={`边界图片${index + 1}`} value={identifier} disabled={busy} onChange={(event) => setFrames((previous) => previous.map((value, position) => position === index ? event.target.value : value))}><option value="">选择项目中的实际图片</option>{data.images.map((image) => <option key={image.assetId} value={image.assetId}>{image.name}</option>)}</select>{identifier && <img alt={`边界图片${index + 1}`} src={assetContentUrl(identifier)} />}</label>)}
    <div className="material-actions"><button type="button" disabled={busy || frames.length >= 101} onClick={() => changeFrames([...frames, ''])}>添加边界图片</button><button type="button" disabled={busy || frames.length <= 2} onClick={() => changeFrames(frames.slice(0, -1))}>移除最后一张</button></div>
    {segments.map((item, index) => <fieldset key={index} disabled={busy}><legend>片段{index + 1}：图片{index + 1} → 图片{index + 2}</legend><label>名称<input aria-label={`片段${index + 1}名称`} value={item.title} onChange={(event) => changeSegment(index, { title: event.target.value })} /></label><label>画面如何运动<textarea aria-label={`片段${index + 1}运动要求`} value={item.prompt} rows={3} onChange={(event) => changeSegment(index, { prompt: event.target.value })} /></label><label>时长（秒）<input aria-label={`片段${index + 1}时长`} type="number" min={2} max={30} value={item.duration} onChange={(event) => changeSegment(index, { duration: Number(event.target.value) })} /></label><label>清晰度<select aria-label={`片段${index + 1}清晰度`} value={item.resolution} onChange={(event) => changeSegment(index, { resolution: event.target.value })}><option>480P</option><option>720P</option><option>1080P</option></select></label><label>目标片段<select aria-label={`片段${index + 1}目标`} value={item.clipId || ''} onChange={(event) => changeSegment(index, { clipId: event.target.value || undefined })}><option value="">新增视频片段</option>{data.clips.map((clip) => <option key={clip.id} value={clip.id}>{clip.name || '现有视频片段'} · {clip.duration}秒</option>)}</select></label></fieldset>)}
    <button type="button" className="material-primary" disabled={busy || frames.some((value) => !value) || segments.some((value) => !value.prompt.trim() || !value.title.trim())} onClick={() => { void perform(save); }}>保存连续视频序列</button>
  </details>;
}

export default function MediaCandidates({ projectId, sceneId, conversationId, onGenerationConfirmed }: {
  projectId: string; sceneId: string; conversationId: string; onGenerationConfirmed: (id: string) => void | Promise<void>;
}) {
  const [data, setData] = useState<CandidateData | null>(null);
  const [busy, setBusy] = useState(false);
  const operationRef = useRef<{ key: string; id: string } | null>(null);
  const reads = useRef({ version: 0 });
  const workspaceTasks = useMaterialWorkspace((state) => state.videoResults?.tasks);
  const workspaceRevision = useMaterialWorkspace((state) => state.conversation?.revision);
  const workspaceProgress = workspaceTasks?.map((task) => `${task.id}:${task.status}:${task.assetId}`).join(',');
  const refresh = useCallback(async () => {
    const current = reads.current;
    const request = ++current.version;
    try {
      const next = await getMediaCandidates(projectId, sceneId, conversationId);
      if (request === current.version) setData(next);
    } catch (problem) {
      if (request === current.version) throw problem;
    }
  }, [projectId, sceneId, conversationId]);
  const load = useCallback(async () => {
    try { await refresh(); }
    catch (problem) { useMaterialWorkspace.setState({ error: problem instanceof Error ? problem.message : '候选读取失败' }); }
  }, [refresh]);
  useEffect(() => {
    const current = reads.current;
    void load();
    return () => { current.version++; };
  }, [load, workspaceProgress, workspaceRevision]);
  const needsCandidatePolling = data?.candidates.some((candidate) => activeCandidateStatuses.has(candidate.status)
    && !workspaceTasks?.some((task) => task.id === candidate.generationId && activeCandidateStatuses.has(task.status)));
  useTaskPolling(!busy && Boolean(needsCandidatePolling), 2500, load);
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    try { await action(); await refresh(); await syncProjectSession(projectId); await useMaterialWorkspace.getState().refresh(); operationRef.current = null; useMaterialWorkspace.setState({ error: '' }); }
    catch (problem) { useMaterialWorkspace.setState({ error: problem instanceof Error ? problem.message : '素材操作失败' }); }
    finally { setBusy(false); }
  }
  function operation(key: string) {
    const value = operationRef.current?.key === key ? operationRef.current : { key, id: operationId() };
    operationRef.current = value;
    return value.id;
  }
  async function prepare(sequence: MediaSequence, key: string) {
    if (data) await quoteSequenceSegment(projectId, sequence.id, key, data.revision, operation(`quote:${sequence.id}:${key}:${data.revision}`));
  }
  async function check(candidate: MediaCandidate) {
    if (!data) return;
    const mode = data.clips.some((clip) => clip.id === candidate.clipId) ? 'replace' : 'append';
    await checkMediaAdoption(projectId, sceneId, candidate, data.revision, mode, operation(`check:${candidate.id}:${data.revision}`));
  }
  if (!data) return <p className="material-subtle">正在读取视频候选…</p>;
  const pending = data.adoptions.filter((item) => ['ready', 'duration_conflict', 'blocked', 'stale'].includes(item.status));
  return <section className="material-media-candidates" data-testid="material-media-candidates"><h4>连续视频与候选</h4>
    <SequenceBuilder data={data} conversationId={conversationId} busy={busy} perform={perform} />
    {data.sequences.map((sequence) => <details key={sequence.id} open className="material-sequence"><summary>{sequence.title} · {sequence.frames.length}张图片 / {sequence.segments.length}段视频</summary>{sequence.segments.map((segment) => <article key={segment.key} className="material-segment"><strong>{segment.title} · {segment.duration}秒 · {segment.resolution}</strong><div className="material-video-boundaries"><figure><img alt={`${segment.title}首帧`} src={assetContentUrl(segment.firstFrame.assetId)} /><figcaption>首帧</figcaption></figure><figure><img alt={`${segment.title}尾帧`} src={assetContentUrl(segment.lastFrame.assetId)} /><figcaption>尾帧</figcaption></figure></div><button type="button" disabled={busy} onClick={() => { void perform(() => prepare(sequence, segment.key)); }}>准备本段视频</button></article>)}</details>)}
    {data.candidates.map((candidate) => <article key={candidate.id} className="material-candidate" data-candidate-id={candidate.id}><strong>{candidate.title}</strong><span className="material-subtle">{candidate.adopted ? '当前已采用' : taskLabels[candidate.status] || candidate.status}{candidate.durationSeconds ? ` · ${candidate.durationSeconds.toFixed(2)}秒` : ''}</span>
      {candidate.status === 'ready' && candidate.assetId && <><video controls playsInline preload="metadata" src={assetContentUrl(candidate.assetId)} /><button type="button" disabled={busy || candidate.adopted} onClick={() => { void perform(() => check(candidate)); }}>{candidate.adopted ? '已采用到目标片段' : '检查并采用此候选'}</button></>}
      {candidate.status === 'awaiting_confirmation' && <>{candidate.frames.length > 0 && <div className="material-video-boundaries">{candidate.frames.map((frame) => <figure key={frame.type}><img alt={frame.type === 'first_frame' ? '待确认首帧' : '待确认尾帧'} src={assetContentUrl(frame.assetId)} /><figcaption>{frame.type === 'first_frame' ? '首帧' : '尾帧'}</figcaption></figure>)}</div>}<p>{candidate.resolution} · 核对首尾画面后确认生成</p><button type="button" className="material-primary" disabled={busy || candidate.projectRevision !== data.revision} onClick={() => { void perform(async () => { await confirmMediaCandidate(candidate, data.revision); await onGenerationConfirmed(candidate.id); }); }}>确认生成本段视频</button>{candidate.projectRevision !== data.revision && <p>项目已更新，请使用“准备本段视频”重新核对。</p>}</>}
    </article>)}
    {pending.map((item) => <article key={item.id} className="material-review" data-adoption-status={item.status}><strong>{item.targetName} · {item.status === 'ready' ? '等待采用确认' : item.status === 'stale' ? '需要重新检查' : '需要处理素材冲突'}</strong><p>{item.message}</p><p>候选时长 {item.availableSeconds.toFixed(2)} 秒；现有裁切需要覆盖到第 {item.requiredSeconds.toFixed(2)} 秒。</p>{item.status === 'ready' && <button type="button" className="material-primary" disabled={busy} onClick={() => { void perform(async () => { await applyMediaAdoption(projectId, item, operation(`apply:${item.id}`)); }); }}>确认采用，保留现有编辑</button>}<button type="button" disabled={busy} onClick={() => { void perform(async () => { await keepMediaVersion(projectId, item); }); }}>保留当前版本</button>{item.status !== 'ready' && <p>可使用场景时间线或本人Codex调整目标片段，再点击候选的“检查并采用此候选”。</p>}</article>)}
    {!data.publishAllowed && <p role="status">项目仍有待处理的素材冲突，发布检查会阻止通过。</p>}
  </section>;
}
