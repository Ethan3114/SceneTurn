import { Modal } from 'antd';
import { ArrowLeft, Film } from 'lucide-react';
import { assetContentUrl, type GenerationView } from '@/app/_utils/cloudApi';
import { videoSourceLabels } from './canvas-video-settings';
import './generation-review.css';

export const taskLabels: Record<string, string> = {
  awaiting_confirmation: '等待确认生成', queued: '等待顺序执行', submitting: '正在提交',
  submission_unknown: '提交结果待核实', processing: '供应商处理中', downloading: '正在保存视频',
  ready: '视频已保存', failed: '生成失败', query_interrupted: '查询中断，可继续查询',
};

type QuoteProps = {
  task: GenerationView;
  revision: number;
  inputDirty: boolean;
  busy: boolean;
  sourceAvailable: boolean;
  onRequote: (sourceNodeId: string) => void;
};

type Props = QuoteProps & {
  onReview: (id: string) => void;
  onAdopt: (task: GenerationView) => void;
  onResume: (task: GenerationView) => void;
  onFocusOutput: (id: string) => void;
};

export function VideoGenerationReview({ task, revision, inputDirty, busy, sourceAvailable, onRequote, onConfirm, onClose, error }: QuoteProps & {
  onConfirm: (task: GenerationView) => void;
  onClose: () => void;
  error: string;
}) {
  const expired = task.projectRevision !== revision;
  const needReview = expired || inputDirty;
  const requoteLabel = inputDirty ? '保存当前输入并重新核对' : '按当前输入重新核对';
  const maximumExceeded = task.quotedCents !== undefined && task.quotedCents > Number(task.input.maxAllowedCents);
  const media = task.input.inputs?.filter((input) => input.type !== 'text' && input.assetId) ?? [];
  const sourceMode = task.input.videoInputMode;
  const modeLabel = typeof sourceMode === 'string' && Object.hasOwn(videoSourceLabels, sourceMode) ? videoSourceLabels[sourceMode as keyof typeof videoSourceLabels]
    : media.some((item) => item.label === '首帧') ? videoSourceLabels.frames : videoSourceLabels.text;
  const model = task.input.model === 'wan3.0-video' ? 'Wan 3.0' : task.input.model;
  const ratio = task.input.ratio === 'adaptive' ? '自适应' : task.input.ratio;

  return <Modal
    title="核对视频生成"
    open
    centered
    width={720}
    footer={null}
    onCancel={onClose}
    keyboard={!busy}
    mask={{ closable: !busy }}
    closable={!busy}
    className="fmv-video-review-dialog"
    styles={{ container: { padding: 0 }, header: { margin: 0, padding: '20px 24px 0' } }}
  >
    <div className="fmv-video-review" data-testid="generation-review" data-task-id={task.id} data-quote-state={expired ? 'expired' : inputDirty ? 'unsaved' : task.status}>
      <div className="fmv-video-review-inputs" data-testid="generation-quote-details">
        <p className="fmv-video-review-specs"><Film size={16} aria-hidden="true" />{[model, modeLabel, task.input.resolution, `${task.input.duration} 秒`, ratio, `${task.input.count ?? 1} 段`].filter(Boolean).join(' · ')}</p>
        {media.length > 0 && <ul aria-label="本次使用的媒体" data-testid="generation-media-inputs" data-count={media.length}>
          {media.map((item) => <li key={item.nodeId}>
            {item.type === 'image' && <img src={assetContentUrl(item.assetId!)} alt={item.label || item.title} />}
            {item.type === 'video' && <video controls playsInline preload="metadata" src={assetContentUrl(item.assetId!)} />}
            <span title={item.title}>{[item.label, item.title].filter(Boolean).join(' · ')}</span>
          </li>)}
        </ul>}
        <h3>实际提示词与镜头剧本</h3>
        <pre data-testid="generation-composed-input">{task.input.prompt}</pre>
        {expired && <p data-testid="quote-expired-message" role="status">当前生成输入对应项目版本 {task.projectRevision}，项目已更新到版本 {revision}。请点击「{requoteLabel}」。</p>}
        {inputDirty && <p data-testid="quote-unsaved-message">画布有未保存的内容或连线。重新核对会保存编辑并显示实际生成输入。</p>}
        {needReview && !sourceAvailable && <p>来源节点已移除，请选择当前文字或生成节点，点击「核对生成内容」。</p>}
        {maximumExceeded && <p role="alert">当前参数超过生成限制，请调整时长或清晰度。</p>}
        {error && <p role="alert">{error}</p>}
      </div>
      <footer className="fmv-video-review-actions">
        <button type="button" onClick={onClose} disabled={busy}><ArrowLeft size={15} aria-hidden="true" />返回编辑</button>
        {needReview && sourceAvailable && <button type="button" data-testid="generation-requote" className="fmv-video-review-primary" disabled={busy} onClick={() => onRequote(task.sourceNodeId)}>{requoteLabel}</button>}
        {!needReview && <button type="button" data-testid="generation-confirm" className="fmv-video-review-primary" disabled={busy || maximumExceeded} onClick={() => onConfirm(task)}>确认生成</button>}
      </footer>
    </div>
  </Modal>;
}

export function GenerationTaskCard({ task, revision, inputDirty, busy, sourceAvailable, onReview, onRequote, onAdopt, onResume, onFocusOutput }: Props) {
  const expired = task.status === 'awaiting_confirmation' && task.projectRevision !== revision;
  const localJoin = task.input.provider === 'local-ffmpeg';
  return <article className="fmv-node-task" data-testid="generation-task" data-task-id={task.id} data-quote-state={expired ? 'expired' : inputDirty && task.status === 'awaiting_confirmation' ? 'unsaved' : task.status}>
    <h3>{expired ? '生成输入需要重新核对' : localJoin && task.status === 'ready' ? '成片已保存' : taskLabels[task.status] || task.status}</h3>
    {localJoin && <p>本机 FFmpeg 视频拼接，不调用模型，无生成费用。</p>}
    {task.status === 'awaiting_confirmation' && <button type="button" data-testid="generation-open-review" className="fmv-node-primary" disabled={busy} onClick={() => onReview(task.id)}>查看生成核对</button>}
    {task.error && <p role="alert">{typeof task.error === 'string' ? task.error : task.error.message}</p>}
    {task.status === 'ready' && task.assetId && <>
      <video controls playsInline preload="metadata" src={assetContentUrl(task.assetId)} />
      <button data-testid="generation-adopt" disabled={busy} onClick={() => onAdopt(task)}>采用到固定场景片段</button>
    </>}
    {['query_interrupted', 'processing', 'downloading', 'submission_unknown'].includes(task.status) && task.remoteTaskId && <button data-testid="generation-resume" disabled={busy} onClick={() => onResume(task)}>继续查询已有任务</button>}
    {task.status === 'failed' && sourceAvailable && <button data-testid="generation-requote" disabled={busy} onClick={() => onRequote(task.sourceNodeId)}>{localJoin ? '按当前连接再次拼接' : '按当前输入重新核对'}</button>}
    {task.outputNodeId && <button data-testid="generation-focus-output" onClick={() => onFocusOutput(task.outputNodeId!)}>查看结果节点</button>}
    <details><summary>任务详情</summary><dl><dt>任务</dt><dd>{task.id}</dd><dt>场景</dt><dd>{task.sceneId}</dd><dt>片段</dt><dd>{task.clipId}</dd><dt>来源节点</dt><dd>{task.sourceNodeId}</dd><dt>结果节点</dt><dd>{task.outputNodeId || '历史任务'}</dd><dt>保存版本</dt><dd>{task.projectRevision}</dd>{localJoin ? <><dt>处理程序</dt><dd>本机 FFmpeg</dd><dt>输入总时长</dt><dd>{task.input.duration} 秒</dd></> : <><dt>地区</dt><dd>{String(task.input.region)}</dd></>}{task.remoteTaskId && <><dt>远端任务</dt><dd>{task.remoteTaskId}</dd></>}</dl>{localJoin && <><h4>视频拼接顺序</h4><ol>{task.input.inputs?.map((input) => <li key={input.nodeId}>{input.title} · {input.durationMs === undefined ? '时长待核实' : `${input.durationMs / 1000} 秒`}<br />素材：{input.assetId}</li>)}</ol></>}{task.input.sourcePrompt !== undefined && <><h4>来源原文</h4><pre>{task.input.sourcePrompt}</pre></>}</details>
  </article>;
}
