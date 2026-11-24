import { memo } from 'react';

import type { TextGenerationView } from '@/app/_utils/cloudApi';
import { findDramaTask } from './drama-tasks';

export const textGenerationStatus: Record<string, string> = { queued: '等待顺序执行', starting: '正在准备文字任务', submitting: '正在提交文字任务', running: '模型正在生成文字', ready: '文字已生成', interrupted: '文字任务已中断', submission_unknown: '提交结果待核实', failed: '文字生成失败', conflict: '文字已生成，等待采用' };
type Props = { task: TextGenerationView; busy: boolean; inline?: boolean; onStop: (task: TextGenerationView) => void; onResume: (task: TextGenerationView) => void; onApply: (task: TextGenerationView) => void; onExpandPlot: (task: TextGenerationView) => void; onFocusOutput: (id: string) => void };

function TextGenerationTaskCardView({ task, busy, inline, onStop, onResume, onApply, onExpandPlot, onFocusOutput }: Props) {
  const tokenCount = (key: string) => typeof task.usage?.[key] === 'number' ? String(task.usage[key]) : '待核实';
  return <article className="fmv-node-task" data-testid="text-generation-task" data-task-id={task.id} data-task-status={task.status}>
    <h3>{findDramaTask(task.task).label} · {textGenerationStatus[task.status] || task.status}</h3>
    {task.error && <p role="alert">{typeof task.error === 'string' ? task.error : task.error.message}</p>}
    {task.outputText && (!inline || task.appliedRevision === null) && <><h4>文字结果</h4><pre data-testid="text-generation-result">{task.outputText}</pre></>}
    <dl data-testid="text-generation-usage"><dt>模型</dt><dd>{task.provider} · {task.model}</dd><dt>输入 Token</dt><dd>{tokenCount('prompt_tokens')}</dd><dt>输出 Token</dt><dd>{tokenCount('completion_tokens')}</dd><dt>合计 Token</dt><dd>{tokenCount('total_tokens')}</dd><dt>缓存命中 Token</dt><dd>{tokenCount('prompt_cache_hit_tokens')}</dd><dt>缓存未命中 Token</dt><dd>{tokenCount('prompt_cache_miss_tokens')}</dd></dl>
    {['queued', 'starting', 'submitting', 'running'].includes(task.status) && <button data-testid="text-generation-stop" disabled={busy} onClick={() => onStop(task)}>停止文字任务</button>}
    {['interrupted', 'submission_unknown'].includes(task.status) && <button data-testid="text-generation-resume" disabled={busy} onClick={() => onResume(task)}>核对已有文字任务</button>}
    {task.status === 'conflict' && task.outputText && <><p>画布在生成期间已有新的保存。核对文字后，可采用到原结果节点。</p><button data-testid="text-generation-apply" disabled={busy} onClick={() => onApply(task)}>采用文字结果</button></>}
    {task.appliedRevision !== null && <p>已保存到项目版本 {task.appliedRevision}，结果文字可继续编辑和连接。</p>}
    {task.task === 'plot' && task.result && 'shots' in task.result && <button data-testid="text-generation-expand-plot" disabled={busy} onClick={() => onExpandPlot(task)}>展开分镜流程</button>}
    <button data-testid="text-generation-focus-output" onClick={() => onFocusOutput(task.outputNodeId)}>查看文字结果</button>
    <details><summary>文字任务详情</summary><dl><dt>任务</dt><dd>{task.id}</dd><dt>提交版本</dt><dd>{task.projectRevision}</dd><dt>来源节点</dt><dd>{task.sourceNodeId}</dd><dt>结果节点</dt><dd>{task.outputNodeId}</dd><dt>供应商请求</dt><dd>{task.remoteRequestId || '尚未取得'}</dd></dl><h4>本次完整输入</h4><pre>{task.input.prompt}</pre></details>
  </article>;
}

export const TextGenerationTaskCard = memo(TextGenerationTaskCardView);
