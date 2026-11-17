import { useState } from 'react';
import { Button, Modal, Popover } from 'antd';
import { ArrowUp, Maximize2, Settings2 } from 'lucide-react';

import { canvasThemes } from './canvas-theme';
import { useThemeStore } from './use-theme-store';
import type { CanvasNodeData, CanvasNodeMetadata } from './canvas';
import type { CanvasResourceReference } from './canvas-resource-references';
import { CanvasNodeReferenceBar } from './canvas-node-reference-bar';
import { CanvasPromptChipInput } from './canvas-prompt-chip-input';

export type CanvasNodePromptPanelProps = {
  node: CanvasNodeData;
  nodes: CanvasNodeData[];
  connectedNodes: CanvasNodeData[];
  mentionReferences: CanvasResourceReference[];
  model: string;
  busy: boolean;
  canGenerate: boolean;
  inputProblem?: string;
  onPromptChange: (nodeId: string, prompt: string) => void;
  onConfigChange: (nodeId: string, patch: Partial<CanvasNodeMetadata>) => void;
  onGenerate: (nodeId: string) => void;
  onDisconnectReference: (fromNodeId: string, toNodeId: string) => void;
  onStartReferenceSelection: (nodeId: string) => void;
};

export function CanvasVideoSettings({ node, model, onConfigChange }: Pick<CanvasNodePromptPanelProps, 'node' | 'model' | 'onConfigChange'>) {
  const change = (patch: Partial<CanvasNodeMetadata>) => onConfigChange(node.id, patch);
  return <div className="fmv-node-video-settings" data-canvas-no-zoom>
    <label>模型<span>{model || '请在生成设置中配置 Wan'}</span></label>
    <label>清晰度<select aria-label="清晰度" value={node.metadata?.vquality || '720P'} onChange={(event) => change({ vquality: event.target.value })}>{['480P', '720P', '1080P'].map((value) => <option key={value}>{value}</option>)}</select></label>
    <label>视频时长<select aria-label="视频时长" value={node.metadata?.seconds || '5'} onChange={(event) => change({ seconds: event.target.value })}>{[3, 5, 8, 10, 15].map((value) => <option value={value} key={value}>{value} 秒</option>)}</select></label>
    <p>数量 1 个。报价将展示本次实际生效的全部参数。</p>
  </div>;
}

export function CanvasNodePromptPanel({ node, nodes, connectedNodes, mentionReferences, model, busy, canGenerate, inputProblem, onPromptChange, onConfigChange, onGenerate, onDisconnectReference, onStartReferenceSelection }: CanvasNodePromptPanelProps) {
  const theme = canvasThemes[useThemeStore((state) => state.theme)];
  const [expanded, setExpanded] = useState(false);
  const prompt = node.metadata?.composerContent ?? node.metadata?.prompt ?? '';
  return <div data-canvas-no-zoom className="rounded-2xl border p-3 shadow-2xl backdrop-blur" style={{ background: theme.toolbar.panel, borderColor: theme.toolbar.border, color: theme.node.text }} onMouseDown={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()} onWheel={(event) => event.stopPropagation()}>
    <CanvasNodeReferenceBar nodeId={node.id} nodes={nodes} connectedNodes={connectedNodes} onDisconnect={onDisconnectReference} onStartSelection={onStartReferenceSelection} />
    <CanvasPromptChipInput value={prompt} references={mentionReferences} onChange={(value) => onPromptChange(node.id, value)} onSubmit={() => { if (canGenerate && !busy) onGenerate(node.id); }} className="thin-scrollbar h-32 w-full resize-none rounded-xl px-3 py-2 text-sm leading-5" placeholder="描述画面、动作和镜头；输入 @ 引用已连接文字" />
    {inputProblem ? <p className="mt-2 text-xs opacity-75" data-testid="generation-input-problem" role="status">{inputProblem}</p> : null}
    <div className="mt-2 flex min-w-0 flex-wrap items-center justify-between gap-2">
      <div className="flex min-w-0 items-center gap-2">
        <Button type="text" aria-label="展开提示词编辑" icon={<Maximize2 size={15} />} onClick={() => setExpanded(true)} />
        <span className="max-w-36 truncate text-xs" title={model}>{model || 'Wan 尚未配置'}</span>
        <Popover trigger="click" placement="top" content={<CanvasVideoSettings node={node} model={model} onConfigChange={onConfigChange} />}><Button type="text" icon={<Settings2 size={15} />} aria-label="视频参数">{node.metadata?.vquality || '720P'} · {node.metadata?.seconds || '5'}秒</Button></Popover>
      </div>
      <Button type="primary" icon={<ArrowUp size={15} />} data-testid="generation-quote" disabled={busy || !canGenerate} onClick={() => onGenerate(node.id)}>保存输入并查看报价</Button>
    </div>
    <Modal title="视频提示词" open={expanded} footer={null} onCancel={() => setExpanded(false)} width={760}>
      <CanvasNodeReferenceBar nodeId={node.id} nodes={nodes} connectedNodes={connectedNodes} onDisconnect={onDisconnectReference} onStartSelection={(id) => { setExpanded(false); onStartReferenceSelection(id); }} />
      <CanvasPromptChipInput value={prompt} references={mentionReferences} onChange={(value) => onPromptChange(node.id, value)} className="h-[50dvh] min-h-64 w-full p-4 text-sm" placeholder="描述视频内容" testId="generation-prompt-expanded" />
    </Modal>
  </div>;
}
