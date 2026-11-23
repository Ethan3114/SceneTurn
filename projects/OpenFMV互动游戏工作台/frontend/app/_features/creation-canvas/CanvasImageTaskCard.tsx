import type { CanvasImageJob } from '@/app/_utils/canvasImageApi';

export const canvasImageStatus: Record<CanvasImageJob['status'], string> = {
  awaiting_confirmation: '等待确认',
  queued: '排队中',
  claimed: '准备生成',
  submitting: '正在生成',
  response_saved: '正在保存图片',
  waiting_connection: '等待个人 Codex 连接',
  submission_unknown: '提交结果待核实',
  ready: '图片已保存',
  failed: '生成失败',
  cancelled: '已取消',
};

export function CanvasImageTaskCard({ task, name, busy, canConfirm, onConfirm, onFocus }: {
  task: CanvasImageJob;
  name: string;
  busy: boolean;
  canConfirm: boolean;
  onConfirm: (task: CanvasImageJob) => void;
  onFocus: (nodeId: string) => void;
}) {
  return <article className="fmv-node-task" data-testid="canvas-image-task">
    <h3>图片生成 · {canvasImageStatus[task.status]}</h3>
    <p>{name} · {task.width} × {task.height} · 1 张</p>
    {task.status === 'awaiting_confirmation' && <>
      <p>{canConfirm ? '确认后会使用所选模型生成这张图片。' : '画布内容已有修改，请重新核对生成内容。'}</p>
      <button className="fmv-node-primary" data-testid="canvas-image-confirm" disabled={busy || !canConfirm} onClick={() => onConfirm(task)}>确认生成图片</button>
    </>}
    {task.status === 'ready' && <button onClick={() => onFocus(task.outputNodeId)}>定位结果</button>}
    {task.errorMessage && <p role="status">{task.errorMessage}</p>}
  </article>;
}
