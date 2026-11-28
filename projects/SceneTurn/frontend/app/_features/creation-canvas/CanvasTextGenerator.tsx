'use client';

import { useRef, useState } from 'react';
import { ArrowUp, Check, ChevronDown, FileText, Languages, Plus, X } from 'lucide-react';
import { Modal } from 'antd';
import type { SceneTurnAsset } from '@/app/_types';
import type { ModelProfile } from '@/app/_utils/providerApi';
import { assetContentUrl } from '@/app/_utils/cloudApi';
import type { CanvasNodeData } from './upstream/canvas';
import { CanvasConfigComposer } from './upstream/canvas-config-composer';
import type { NodeGenerationInput } from './upstream/canvas-node-generation';
import './canvas-script-table.css';

interface Props {
  node: CanvasNodeData;
  nodes: CanvasNodeData[];
  inputs: NodeGenerationInput[];
  connected: CanvasNodeData[];
  images: SceneTurnAsset[];
  models: ModelProfile[];
  model?: ModelProfile;
  status: string;
  disabled: boolean;
  inputProblem: string;
  onModel: (id: string) => void;
  onPrompt: (value: string) => void;
  onChooseCanvas: () => void;
  onUpload: (files: File[]) => void;
  onLibrary: (assets: SceneTurnAsset[]) => void;
  onReorder: (from: string, to: string) => void;
  onRemove: (from: string) => void;
  onGenerate: (translate?: boolean) => void;
}

export function CanvasTextGenerator({ node, nodes, inputs, connected, images, models, model, status, disabled, inputProblem, onModel, onPrompt, onChooseCanvas, onUpload, onLibrary, onReorder, onRemove, onGenerate }: Props) {
  const [menu, setMenu] = useState<'add' | 'model' | null>(null);
  const [library, setLibrary] = useState<string[] | null>(null);
  const upload = useRef<HTMLInputElement>(null);
  const dragId = useRef<string | null>(null);
  return <section className="fmv-text-generator" data-testid="generation-config" data-kind="text" aria-label="文本生成面板" onKeyDown={(event) => { if (event.key === 'Escape') { setMenu(null); event.stopPropagation(); } }}>
    <div className="fmv-text-refs">
      <div className="fmv-text-menu-wrap">
        <button type="button" aria-label="添加参考" aria-expanded={menu === 'add'} onClick={() => setMenu(menu === 'add' ? null : 'add')}><Plus size={16} /></button>
        {menu === 'add' && <div className="fmv-text-menu">
          <button type="button" onClick={() => { setMenu(null); onChooseCanvas(); }}>画布参考</button>
          <button type="button" onClick={() => { setMenu(null); upload.current?.click(); }}>本地上传</button>
          <button type="button" onClick={() => { setMenu(null); setLibrary([]); }}>个人素材库</button>
        </div>}
      </div>
      <input ref={upload} type="file" hidden multiple accept="image/png,image/jpeg,image/webp,image/gif" onChange={(event) => { const files = Array.from(event.target.files || []); event.target.value = ''; if (files.length) onUpload(files); }} />
      {connected.map((source, index) => <div className="fmv-text-ref" data-testid="text-reference" key={source.id} draggable tabIndex={0} aria-label={`参考 ${index + 1} ${source.title}`}
        onDragStart={(event) => { dragId.current = source.id; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', source.id); }}
        onDragEnd={() => { dragId.current = null; }}
        onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); if (dragId.current) onReorder(dragId.current, source.id); dragId.current = null; }}
        onKeyDown={(event) => {
          const delta = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0;
          if (delta && connected[index + delta]) { event.preventDefault(); event.stopPropagation(); onReorder(source.id, connected[index + delta].id); }
        }}>
        {source.type === 'image' && source.metadata?.content ? <img src={source.metadata.content} alt={source.title} /> : <FileText size={18} />}
        <span aria-hidden="true">{index + 1}</span><button type="button" aria-label={`移除参考 ${source.title}`} onClick={() => onRemove(source.id)}><X size={11} /></button>
      </div>)}
    </div>
    <CanvasConfigComposer embedded nodeId={node.id} nodes={nodes} inputs={inputs} value={node.metadata?.composerContent ?? node.metadata?.prompt ?? ''} connectedNodes={connected} onChange={onPrompt} />
    <div className="fmv-text-generator-footer">
      <div className="fmv-text-menu-wrap"><button type="button" aria-label="模型" data-testid="generation-model-trigger" aria-expanded={menu === 'model'} onClick={() => setMenu(menu === 'model' ? null : 'model')}><FileText size={14} />{model?.label || '选择模型'}<ChevronDown size={12} /></button>
        {menu === 'model' && <div className="fmv-text-menu" role="listbox" aria-label="文本模型">{models.map((item) => <button type="button" key={item.id} role="option" aria-selected={model?.id === item.id} onClick={() => { onModel(item.id); setMenu(null); }}>{item.label}{item.id === model?.id && <Check size={12} />}</button>)}</div>}
      </div>
      <button type="button" className="fmv-text-translate" title="翻译提示词" aria-label="翻译提示词" disabled={disabled || !node.metadata?.composerContent?.trim()} onClick={() => onGenerate(true)}><Languages size={17} /></button>
      <button type="button" className="fmv-text-submit" data-testid="text-generation-start" aria-label="生成文字" title={inputProblem || status} disabled={disabled || Boolean(inputProblem)} onClick={() => onGenerate()}><ArrowUp size={17} /></button>
    </div>
    <span className="sr-only" data-testid="text-service-status" role="status">{status}</span>
    <Modal open={library !== null} title="个人素材库" cancelText="取消" okText="确认选择" okButtonProps={{ disabled: !library?.length }} onCancel={() => setLibrary(null)} onOk={() => {
      onLibrary((library || []).flatMap((id) => images.filter((asset) => asset.id === id))); setLibrary(null);
    }}>
      <div className="fmv-script-library" data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()} onClick={(event) => event.stopPropagation()}>
        {images.length ? images.map((asset) => <label key={asset.id}><img src={assetContentUrl(asset.id)} alt="" /><input type="checkbox" checked={library?.includes(asset.id) || false} onChange={(event) => {
          const checked = event.currentTarget.checked;
          setLibrary((ids) => checked ? [...(ids || []), asset.id] : (ids || []).filter((id) => id !== asset.id));
        }} />{asset.name}</label>) : <p>素材库中还没有图片。</p>}
      </div>
    </Modal>
  </section>;
}
