import { assetContentUrl, type GenerationView } from '@/app/_utils/cloudApi';

export const taskLabels: Record<string, string> = {
  awaiting_confirmation: '等待确认生成', queued: '等待顺序执行', submitting: '正在提交',
  submission_unknown: '提交结果待核实', processing: '供应商处理中', downloading: '正在保存视频',
  ready: '视频已保存', failed: '生成失败', query_interrupted: '查询中断，可继续查询',
};

type Props = {
  task: GenerationView;
  revision: number;
  inputDirty: boolean;
  busy: boolean;
  sceneTitle: string;
  sourceAvailable: boolean;
  onConfirm: (task: GenerationView) => void;
  onRequote: (sourceNodeId: string) => void;
  onAdopt: (task: GenerationView) => void;
  onResume: (task: GenerationView) => void;
  onFocusOutput: (id: string) => void;
};

export function GenerationTaskCard({ task, revision, inputDirty, busy, sceneTitle, sourceAvailable, onConfirm, onRequote, onAdopt, onResume, onFocusOutput }: Props) {
  const expired = task.status === 'awaiting_confirmation' && task.projectRevision !== revision;
  const requoteLabel = inputDirty ? '保存当前输入并重新核对' : '按当前输入重新核对';
  const maximumExceeded = task.quotedCents > Number(task.input.maxAllowedCents);
  const localJoin = task.input.provider === 'local-ffmpeg';
  return <article className="fmv-node-task" data-testid="generation-task" data-task-id={task.id} data-quote-state={expired ? 'expired' : inputDirty && task.status === 'awaiting_confirmation' ? 'unsaved' : task.status}>
    <h3>{expired ? '生成输入需要重新核对' : localJoin && task.status === 'ready' ? '成片已保存' : taskLabels[task.status] || task.status}</h3>
    {localJoin && <p>本机 FFmpeg 视频拼接，不调用模型，无生成费用。</p>}
    {task.status === 'awaiting_confirmation' && <div data-testid="generation-quote-details">
      <dl><dt>目标场景</dt><dd>{sceneTitle}</dd><dt>目标片段</dt><dd>本次绑定的视频片段</dd><dt>供应商与模型</dt><dd>阿里云 Wan · {String(task.input.model)}</dd><dt>视频参数</dt><dd>{task.input.resolution} · {task.input.duration}秒 · {String(task.input.ratio)} · {String(task.input.count)}个</dd><dt>音频</dt><dd>{task.input.audio ? '启用' : '关闭'}</dd><dt>提示词扩写</dt><dd>{task.input.promptExtend ? '启用' : '关闭'}</dd><dt>水印</dt><dd>{task.input.watermark ? '启用' : '关闭'}</dd></dl>
      <h4>实际完整输入</h4><pre data-testid="generation-composed-input">{task.input.prompt}</pre>
      <h4>参考文字</h4>{task.input.inputs?.length ? <ul>{task.input.inputs.map((input) => <li key={input.nodeId}><strong>{input.label} · {input.title}</strong><pre>{input.text}</pre></li>)}</ul> : <p>本次没有引用其他文字节点。</p>}
      <p>确认后提交真实视频生成请求。</p>
      {expired && <p data-testid="quote-expired-message" role="status">当前生成输入对应项目版本 {task.projectRevision}，项目已更新到版本 {revision}。请点击“{requoteLabel}”核对实际生成内容。</p>}
      {inputDirty && <p data-testid="quote-unsaved-message">画布中有未保存的内容或连线。重新核对会保存这些编辑，并显示实际生成输入。</p>}
      {(expired || inputDirty) && sourceAvailable && <button data-testid="generation-requote" className="fmv-node-primary" disabled={busy} onClick={() => onRequote(task.sourceNodeId)}>{requoteLabel}</button>}
      {(expired || inputDirty) && !sourceAvailable && <p>来源节点已移除，请选择当前文字或生成节点，点击“核对生成内容”。</p>}
      {!expired && !inputDirty && <button data-testid="generation-confirm" className="fmv-node-primary" disabled={busy || maximumExceeded} onClick={() => onConfirm(task)}>确认生成</button>}
      {maximumExceeded && <p role="alert">当前参数超过生成限制，请调整时长或清晰度。</p>}
    </div>}
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
