'use client';

import { useRef, useState } from 'react';
import { InputNumber, Modal } from 'antd';
import { Plus, Trash2 } from 'lucide-react';
import { StoryboardEditor } from './StoryboardEditor';

import './canvas-script-table.css';

export type CanvasScriptRow = {
  id: string;
  duration: number | null;
  title: string;
  visual: string;
  action: string;
  dialogue: string;
  referenceNodeId?: string;
  shotTextId?: string;
  imageConfigId?: string;
  videoConfigId?: string;
  shotSize?: string;
  lighting?: string;
  sound?: string;
  camera?: string;
  finalPrompt?: string;
};

type ScriptRowValues = Omit<CanvasScriptRow, 'id'>;

export function createCanvasScriptRow(values: Partial<ScriptRowValues> = {}): CanvasScriptRow {
  return { id: crypto.randomUUID(), duration: null, title: '', visual: '', action: '', dialogue: '', ...values };
}

const textColumns = [
  { field: 'visual', title: '画面描述' },
  { field: 'action', title: '动作' },
  { field: 'dialogue', title: '对白' },
] as const;

export interface ScriptTableProps {
  rows: CanvasScriptRow[];
  onChange: (rows: CanvasScriptRow[]) => void;
  onClose: () => void;
  onPrepare?: (ids: string[], kind: 'image' | 'video') => void;
  images?: { id: string; title: string; url: string }[];
  title?: string;
  legacy?: boolean;
}

export function CanvasScriptTable(props: ScriptTableProps) {
  return props.legacy ? <LegacyScriptTable {...props} /> : <StoryboardEditor {...props} />;
}

function LegacyScriptTable({ rows, onChange, onClose, onPrepare, images = [], title = '分镜脚本' }: ScriptTableProps) {
  const tableRef = useRef<HTMLTableElement>(null);
  const addRef = useRef<HTMLButtonElement>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const selected = rows.filter((row) => selectedIds.includes(row.id)).map((row) => row.id);

  function focusRow(index: number) {
    requestAnimationFrame(() => {
      const input = tableRef.current?.querySelector<HTMLInputElement>(`[data-shot-index="${index}"] input[type="text"]`);
      (input ?? addRef.current)?.focus();
    });
  }

  function updateRow(id: string, values: Partial<ScriptRowValues>) {
    onChange(rows.map((row) => row.id === id ? { ...row, ...values } : row));
  }

  function addRow() {
    onChange([...rows, createCanvasScriptRow()]);
    focusRow(rows.length);
  }

  function removeRow(id: string, index: number) {
    onChange(rows.filter((row) => row.id !== id));
    focusRow(Math.min(index, rows.length - 2));
  }

  return (
    <Modal
      title={title}
      open
      centered
      width="min(1080px, calc(100vw - 64px))"
      className="fmv-canvas-script-dialog"
      onCancel={onClose}
      footer={null}
      maskClosable={false}
    >
      <div data-testid="canvas-script-table" data-canvas-no-zoom>
        <div className="fmv-canvas-script-scroll" aria-label="分镜脚本表格">
          <table ref={tableRef} className="fmv-canvas-script-table">
            <colgroup>
              <col className="fmv-canvas-script-index" />
              <col className="fmv-canvas-script-duration" />
              <col className="fmv-canvas-script-title" />
              <col className="fmv-canvas-script-visual" />
              <col className="fmv-canvas-script-action" />
              <col />
              <col className="fmv-canvas-script-remove" />
            </colgroup>
            <thead>
              <tr>
                <th scope="col"><input type="checkbox" aria-label="选择全部镜头" checked={rows.length > 0 && selected.length === rows.length} onChange={(event) => setSelectedIds(event.target.checked ? rows.map((row) => row.id) : [])} /></th>
                <th scope="col">时长</th>
                <th scope="col">镜头</th>
                {textColumns.map(({ field, title }) => <th key={field} scope="col">{title}</th>)}
                <th scope="col"><span className="sr-only">操作</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={row.id} data-shot-index={index} data-testid="canvas-script-row">
                  <th scope="row"><label className="fmv-script-select-row"><input type="checkbox" checked={selected.includes(row.id)} aria-label={`选择镜头 ${index + 1}`} onChange={(event) => setSelectedIds(event.target.checked ? [...selected, row.id] : selected.filter((id) => id !== row.id))} />{index + 1}</label></th>
                  <td>
                    <div className="fmv-canvas-script-duration-field">
                      <InputNumber
                        value={row.duration}
                        min={0.1}
                        step={0.1}
                        controls={false}
                        variant="borderless"
                        aria-label={`镜头 ${index + 1} 时长（秒）`}
                        onChange={(duration) => updateRow(row.id, { duration })}
                      />
                      <span aria-hidden="true">秒</span>
                    </div>
                  </td>
                  <td>
                    <input type="text" aria-label={`镜头 ${index + 1} 名称`} value={row.title} onChange={(event) => updateRow(row.id, { title: event.target.value })} />
                    {images.length > 0 && <label className="fmv-script-frame">
                      {row.referenceNodeId && images.find((image) => image.id === row.referenceNodeId) && <img src={images.find((image) => image.id === row.referenceNodeId)?.url} alt={`镜头 ${index + 1} 分镜图`} />}
                      <select aria-label={`镜头 ${index + 1} 分镜图`} value={row.referenceNodeId || ''} onChange={(event) => updateRow(row.id, { referenceNodeId: event.target.value || undefined })}>
                        <option value="">选择分镜图</option>
                        {images.map((image) => <option key={image.id} value={image.id}>{image.title}</option>)}
                      </select>
                    </label>}
                  </td>
                  {textColumns.map(({ field, title }) => (
                    <td key={field}>
                      <textarea rows={2} aria-label={`镜头 ${index + 1} ${title}`} value={row[field]} onChange={(event) => updateRow(row.id, { [field]: event.target.value })} />
                    </td>
                  ))}
                  <td>
                    <button type="button" className="fmv-canvas-script-delete" aria-label={`删除镜头 ${index + 1}`} onClick={() => removeRow(row.id, index)}>
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="fmv-canvas-script-actions">
          <button ref={addRef} type="button" data-testid="canvas-script-add-row" onClick={addRow}><Plus size={15} aria-hidden="true" />新增镜头</button>
          {onPrepare && <div className="fmv-script-generate">
            <button type="button" disabled={!selected.length} onClick={() => onPrepare(selected, 'image')}>生成分镜图</button>
            <button type="button" disabled={!selected.length} onClick={() => onPrepare(selected, 'video')}>分镜转视频</button>
          </div>}
          <button type="button" className="fmv-canvas-script-done" onClick={onClose}>完成</button>
        </div>
      </div>
    </Modal>
  );
}
