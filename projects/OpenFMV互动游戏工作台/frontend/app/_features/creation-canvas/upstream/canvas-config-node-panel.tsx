import { memo } from 'react';
import Link from 'next/link';

import { Button, Popover } from 'antd';
import { FileText, Film, ImageIcon, Settings2 } from 'lucide-react';

import { DRAMA_TASKS, nodeDramaTask, type DramaTaskKind } from '../drama-tasks';
import { canvasThemes } from './canvas-theme';
import { useThemeStore } from './use-theme-store';
import type { CanvasNodeData, CanvasNodeMetadata } from './canvas';
import type { NodeGenerationInput } from './canvas-node-generation';
import { CanvasConfigComposer } from './canvas-config-composer';
import { CanvasVideoSettings } from './canvas-node-prompt-panel';

type Props = {
  node: CanvasNodeData;
  nodes: CanvasNodeData[];
  connectedNodes: CanvasNodeData[];
  inputs: NodeGenerationInput[];
  model: string;
  textServiceLabel: string;
  textConnected: boolean;
  busy: boolean;
  canGenerate: boolean;
  inputProblem: string;
  joinDuration?: { count: number; seconds: number | null; estimated: boolean };
  onConfigChange: (id: string, patch: Partial<CanvasNodeMetadata>) => void;
  onGenerate: (id: string) => void;
  onDisconnectReference: (fromNodeId: string, toNodeId: string) => void;
  onStartReferenceSelection: (id: string) => void;
};

function CanvasConfigNodePanelView({ node, nodes, connectedNodes, inputs, model, textServiceLabel, textConnected, busy, canGenerate, inputProblem, joinDuration, onConfigChange, onGenerate, onDisconnectReference, onStartReferenceSelection }: Props) {
  const theme = canvasThemes[useThemeStore((state) => state.theme)];
  const task = nodeDramaTask(node);
  const changeKind = (kind: DramaTaskKind) => {
    const next = DRAMA_TASKS.find((item) => item.kind === kind)!;
    onConfigChange(node.id, { generationMode: kind, dramaTask: next.id, ...(next.id === 'text' && !(node.metadata?.composerContent ?? node.metadata?.prompt)?.trim() ? { composerContent: '根据创意编写完整剧本，包含场景、人物动作、对白与互动选择。' } : {}) });
  };
  const resources = inputs.flatMap((input) => input.type === 'group' ? input.children : [input]);
  return <div className="flex h-full w-full cursor-default flex-col overflow-hidden px-3 pb-3 pt-5 text-sm" style={{ color: theme.node.text }} data-testid="generation-config" onMouseDown={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} onWheel={(event) => event.stopPropagation()}>
    <div className="mb-2 flex gap-1" role="group" aria-label="生成类别">{([{ kind: 'text', label: '文字', Icon: FileText }, { kind: 'image', label: '图像', Icon: ImageIcon }, { kind: 'video', label: '视频', Icon: Film }] as const).map(({ kind, label, Icon }) => <Button key={kind} size="small" type={task.kind === kind ? 'primary' : 'text'} data-testid={`generation-kind-${kind}`} onClick={() => changeKind(kind)}><Icon size={13} />{label}</Button>)}</div>
    <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain">
      <div className="grid grid-cols-2 gap-1" role="group" aria-label="生成功能">{DRAMA_TASKS.filter((item) => item.kind === task.kind).map((item) => <Button key={item.id} size="small" type={item.id === task.id ? 'primary' : 'default'} data-testid={`generation-task-${item.id}`} onClick={() => onConfigChange(node.id, { generationMode: item.kind, dramaTask: item.id, ...(item.id === 'text' && !(node.metadata?.composerContent ?? node.metadata?.prompt)?.trim() ? { composerContent: '根据创意编写完整剧本，包含场景、人物动作、对白与互动选择。' } : {}) })}>{item.label}</Button>)}</div>
      <p className="text-xs leading-5 opacity-70">{task.hint}</p>
      <div className="flex flex-wrap gap-2 text-xs opacity-70">{(['text', 'image', 'video', 'audio'] as const).map((kind, index) => <span key={kind}>{['文字', '图片', '视频', '音频'][index]} {resources.filter((item) => item.type === kind).length}</span>)}</div>
      {task.id !== 'join' && <CanvasConfigComposer embedded nodeId={node.id} nodes={nodes} value={node.metadata?.composerContent ?? node.metadata?.prompt ?? ''} inputs={inputs} connectedNodes={connectedNodes} onChange={(composerContent) => onConfigChange(node.id, { composerContent })} onDisconnectReference={onDisconnectReference} onStartReferenceSelection={onStartReferenceSelection} />}
      {task.kind === 'text' && <div><p className="break-words text-xs" data-testid="text-service-status">{textServiceLabel}</p><Link href="/zh-CN/settings/text" className="text-xs underline">文字生成设置</Link></div>}
      {task.kind === 'image' && <p className="rounded-lg border p-2 text-xs" data-testid="image-service-status" role="status">图像生成服务尚未配置。可保存描述与参考，或导入图片后进行切片。</p>}
      {task.kind === 'video' && task.id !== 'join' && <div className="space-y-1"><p className="break-all text-xs">{model || '请在生成设置中配置 Wan'}</p><Popover trigger="click" placement="top" content={<CanvasVideoSettings node={node} model={model} onConfigChange={onConfigChange} />}><Button type="text" icon={<Settings2 size={14} />} aria-label="视频参数">{node.metadata?.vquality || '720P'} · {node.metadata?.seconds || '5'}秒 · 1个</Button></Popover></div>}
      {task.id === 'join' && <p className="text-xs" data-testid="join-service-status">由本机 FFmpeg 按连接顺序处理，不调用模型。文字节点不参与拼接。</p>}
      {task.id === 'join' && joinDuration && <p className="text-xs" data-testid="join-duration-summary" data-duration-seconds={joinDuration.seconds ?? ''}>已连接 {joinDuration.count} 段视频 · {joinDuration.seconds === null ? '部分片段时长等待确定' : `${joinDuration.estimated ? '按当前生成参数预计' : '实际总时长'} ${Number(joinDuration.seconds.toFixed(3))} 秒`}</p>}
      {inputProblem && <p className="text-xs leading-5 opacity-75" data-testid="generation-input-problem" role="status">{inputProblem}</p>}
    </div>
    <Button type="primary" className="mt-3 w-full shrink-0" data-testid={task.kind === 'text' ? 'text-generation-start' : task.id === 'join' ? 'generation-join' : 'generation-quote'} disabled={busy || !canGenerate || task.kind === 'image' || task.kind === 'text' && !textConnected} onClick={() => onGenerate(node.id)}>{task.kind === 'text' ? `生成${task.label}` : task.kind === 'image' ? '图像服务尚未配置' : task.id === 'join' ? '拼接成片' : '核对生成内容'}</Button>
  </div>;
}

export const CanvasConfigNodePanel = memo(CanvasConfigNodePanelView);
