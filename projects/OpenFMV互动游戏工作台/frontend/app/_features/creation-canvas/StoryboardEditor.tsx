'use client';

import { useState } from 'react';
import { Modal } from 'antd';
import { ArrowRight, MoreHorizontal, Plus } from 'lucide-react';
import { createCanvasScriptRow, type CanvasScriptRow, type ScriptTableProps } from './CanvasScriptTable';
import { formatShot } from './canvas-script-actions';

const fields = [
  ['visual', '画面描述'], ['shotSize', '景别'], ['lighting', '光影氛围'], ['dialogue', '对白·旁白'], ['sound', '音效'], ['camera', '运镜'],
] as const;
const stages = ['确认镜头', '准备资产', '合成提示词'];

export function StoryboardEditor({ rows, title, onChange, onClose, onPrepare, images = [] }: ScriptTableProps) {
  const [stage, setStage] = useState(0);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [menu, setMenu] = useState<string>();
  const selected = rows.filter((row) => selectedIds.includes(row.id)).map((row) => row.id);
  const assets = rows.filter((row) => row.referenceNodeId).length;
  const prompts = rows.filter((row) => row.finalPrompt?.trim()).length;
  const complete = [rows.length > 0 && rows.every((row) => row.visual.trim()), rows.length > 0 && assets === rows.length, rows.length > 0 && prompts === rows.length].filter(Boolean).length;
  const captions = [`${rows.length}个镜头待核对`, assets ? `${assets}个镜头已有资产` : '暂无资产', `${prompts}/${rows.length} 已合成`];
  const change = (id: string, patch: Partial<CanvasScriptRow>) => onChange(rows.map((row) => row.id === id ? { ...row, ...patch } : row));
  const choose = (id: string, checked: boolean) => setSelectedIds((values) => checked ? [...values, id] : values.filter((value) => value !== id));
  return <Modal open centered footer={null} onCancel={onClose} title={<span className="sr-only">{title || '分镜脚本'}</span>} width="100vw" className="fmv-storyboard-dialog" maskClosable={false}>
    <div className="fmv-storyboard-editor" data-testid="canvas-script-table">
      <nav className="fmv-storyboard-steps" aria-label="分镜制作步骤">{stages.map((name, index) => <button type="button" key={name} aria-current={stage === index ? 'step' : undefined} onClick={() => { setStage(index); setMenu(undefined); }}>
        <b>{index + 1}</b><span>{name}<small>{captions[index]}</small></span>
      </button>)}</nav>
      <div className="fmv-storyboard-progress">{complete}/3 完成后可批量生视频</div>
      <div className="fmv-storyboard-grid"><div className="fmv-storyboard-scroll">
        <table className="fmv-storyboard-table" data-stage={stage}>
          <thead><tr>
            <th>{stage > 0 ? <input type="checkbox" aria-label="选择全部镜头" checked={!!rows.length && selected.length === rows.length} onChange={(event) => setSelectedIds(event.target.checked ? rows.map((row) => row.id) : [])} /> : '镜号'}</th>
            <th>时长</th>{stage === 0 ? <>{fields.map(([, name]) => <th key={name}>{name}</th>)}<th>最终提示词</th><th>操作</th></> : <><th>画面描述</th><th>分镜图</th>{stage === 2 && <th>最终提示词</th>}</>}
          </tr></thead>
          <tbody>{rows.map((row, i) => {
            const prefix = `镜头 ${i + 1} `;
            return <tr key={row.id} data-testid="canvas-script-row">
              <td>{stage === 0 ? i + 1 : <label><input type="checkbox" checked={selected.includes(row.id)} aria-label={`选择镜头 ${i + 1}`} onChange={(event) => choose(row.id, event.target.checked)} /> {i + 1}</label>}</td>
              <td><label className="fmv-storyboard-duration"><input type="number" min=".1" step=".1" aria-label={prefix + '时长（秒）'} value={row.duration ?? ''} onChange={(event) => change(row.id, { duration: event.target.value ? Number(event.target.value) : null })} />s</label></td>
              {stage === 0 ? <>
                {fields.map(([key, name]) => <td key={key}>{key === 'visual' && <input type="text" className="fmv-storyboard-name" aria-label={prefix + '名称'} value={row.title} onChange={(event) => change(row.id, { title: event.target.value })} />}
                  <textarea aria-label={prefix + name} rows={2} value={row[key] || ''} placeholder="+" onChange={(event) => change(row.id, { [key]: event.target.value })} />
                </td>)}
                <td><textarea aria-label={prefix + '最终提示词'} placeholder="待生成提示词" rows={2} value={row.finalPrompt || ''} onChange={(event) => change(row.id, { finalPrompt: event.target.value })} /></td>
                <td><button type="button" aria-label={prefix + '操作'} onClick={() => setMenu(menu === row.id ? undefined : row.id)}><MoreHorizontal size={18} /></button>
                  {menu === row.id && <div className="fmv-storyboard-row-menu"><button type="button" onClick={() => { onChange(rows.filter((item) => item.id !== row.id)); setMenu(undefined); }}>删除镜头</button></div>}
                </td>
              </> : <>
                <td>{row.title && <strong>{row.title}</strong>}<p>{row.visual}</p>{row.action && <p>{row.action}</p>}</td>
                <td><label className="fmv-script-frame"><select aria-label={prefix + '分镜图'} value={row.referenceNodeId || ''} onChange={(event) => change(row.id, { referenceNodeId: event.target.value || undefined })}>
                  <option value="">选择分镜图</option>{images.map((image) => <option key={image.id} value={image.id}>{image.title}</option>)}
                </select>{row.referenceNodeId && <img alt={prefix + '分镜图'} src={images.find((image) => image.id === row.referenceNodeId)?.url} />}</label></td>
                {stage === 2 && <td><textarea rows={4} aria-label={prefix + '最终提示词'} value={row.finalPrompt || ''} onChange={(event) => change(row.id, { finalPrompt: event.target.value })} /></td>}
              </>}
            </tr>;
          })}</tbody>
        </table>
      </div>
      <footer><button type="button" data-testid="canvas-script-add-row" onClick={() => onChange([...rows, createCanvasScriptRow({ duration: 5 })])}><Plus size={16} />添加镜头</button>
        {stage > 0 && <button type="button" disabled={!selected.length} onClick={() => onPrepare?.(selected, 'image')}>生成分镜图</button>}
        {stage === 2 && <button type="button" disabled={!selected.length} onClick={() => onChange(rows.map((row, index) => selected.includes(row.id) ? { ...row, finalPrompt: formatShot({ ...row, finalPrompt: '' }, index) } : row))}>合成提示词</button>}
        {stage < 2 ? <button type="button" className="fmv-storyboard-next" onClick={() => { setStage(stage + 1); setMenu(undefined); }}><ArrowRight size={14} />下一步：{stages[stage + 1]}</button>
          : <button type="button" className="fmv-storyboard-next" disabled={!selected.length} onClick={() => onPrepare?.(selected, 'video')}>分镜转视频</button>}
      </footer></div>
    </div>
  </Modal>;
}
