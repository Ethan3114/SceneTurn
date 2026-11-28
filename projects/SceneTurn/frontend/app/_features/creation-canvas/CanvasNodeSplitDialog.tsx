import { memo, useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';

import { Button, InputNumber, Modal } from 'antd';
import { Grid2x2, ListRestart, PanelTop, Redo2, Rows3, Trash2, Undo2, ZoomIn, ZoomOut } from 'lucide-react';

import type { ImageSplitParams } from '@/app/_utils/cloudApi';
import { useImageEditorViewport } from './useImageEditorViewport';

type Params = Required<ImageSplitParams>;
type ActiveLine = { axis: 'horizontal' | 'vertical'; index: number } | null;
const defaults: Params = { rows: 2, columns: 2, horizontalLines: [.5], verticalLines: [.5] };
const gridLines = (count: number) => Array.from({ length: count - 1 }, (_, index) => (index + 1) / count);
type Props = { dataUrl: string; open: boolean; busy: boolean; onClose: () => void; onConfirm: (params: ImageSplitParams) => void };

function CanvasNodeSplitDialogView({ dataUrl, open, busy, onClose, onConfirm }: Props) {
  const [params, setParams] = useState<Params>(defaults);
  const [image, setImage] = useState<{ width: number; height: number } | null>(null);
  const [imageError, setImageError] = useState('');
  const [active, setActive] = useState<ActiveLine>(null);
  const [history, setHistory] = useState<Params[]>([]);
  const [redo, setRedo] = useState<Params[]>([]);
  const dragAbort = useRef<AbortController | null>(null);
  const viewport = useImageEditorViewport(image, open);
  const rows = params.horizontalLines.length + 1, columns = params.verticalLines.length + 1;
  const total = rows * columns;
  const remember = () => { setHistory((current) => [...current.slice(-49), structuredClone(params)]); setRedo([]); };
  const update = (axis: 'rows' | 'columns', value: number | null) => {
    if (value === null) return;
    const count = Math.max(1, Math.min(12, Math.round(value)));
    remember(); setActive(null); setParams((current) => ({ ...current, [axis]: count, [axis === 'rows' ? 'horizontalLines' : 'verticalLines']: gridLines(count) }));
  };
  const add = (axis: 'horizontal' | 'vertical') => {
    const key = axis === 'horizontal' ? 'horizontalLines' : 'verticalLines';
    if (params[key].length >= 11) return;
    const cuts = [0, ...params[key], 1];
    let start = 0, gap = 0;
    for (let index = 0; index < cuts.length - 1; index++) if (cuts[index + 1] - cuts[index] > gap) { start = cuts[index]; gap = cuts[index + 1] - start; }
    const spot = start + gap / 2, lines = [...params[key], spot].sort((a, b) => a - b);
    remember(); setActive({ axis, index: lines.indexOf(spot) }); setParams((current) => ({ ...current, [key]: lines }));
  };
  const remove = useCallback(() => {
    if (!active || busy) return;
    setHistory((current) => [...current.slice(-49), structuredClone(params)]); setRedo([]);
    const key = active.axis === 'horizontal' ? 'horizontalLines' : 'verticalLines';
    setParams((current) => ({ ...current, [key]: current[key].filter((_, index) => index !== active.index) })); setActive(null);
  }, [active, busy, params]);
  const undo = useCallback(() => {
    const previous = history.at(-1); if (!previous || busy) return;
    setRedo((current) => [...current, structuredClone(params)]); setHistory((current) => current.slice(0, -1)); setParams(previous); setActive(null);
  }, [history, params, busy]);
  const redoChange = useCallback(() => {
    const next = redo.at(-1); if (!next || busy) return;
    setHistory((current) => [...current, structuredClone(params)]); setRedo((current) => current.slice(0, -1)); setParams(next); setActive(null);
  }, [redo, params, busy]);
  const drag = (axis: 'horizontal' | 'vertical', index: number, event: ReactPointerEvent) => {
    if (busy) return;
    event.preventDefault(); event.stopPropagation(); setActive({ axis, index });
    const box = viewport.stageRef.current?.getBoundingClientRect(); if (!box) return;
    remember(); dragAbort.current?.abort(); const controller = new AbortController(); dragAbort.current = controller;
    const move = (pointer: PointerEvent) => {
      const value = axis === 'horizontal' ? (pointer.clientY - box.top) / box.height : (pointer.clientX - box.left) / box.width;
      const key = axis === 'horizontal' ? 'horizontalLines' : 'verticalLines';
      setParams((current) => { const lines = [...current[key]]; lines[index] = Math.min((lines[index + 1] ?? 1) - .01, Math.max((lines[index - 1] ?? 0) + .01, value)); return { ...current, [key]: lines }; });
    };
    window.addEventListener('pointermove', move, { signal: controller.signal });
    window.addEventListener('pointerup', () => controller.abort(), { signal: controller.signal });
    window.addEventListener('pointercancel', () => controller.abort(), { signal: controller.signal });
  };
  useEffect(() => {
    if (!open) return;
    let active = true;
    const source = new window.Image();
    source.onload = () => { if (active) setImage({ width: source.naturalWidth, height: source.naturalHeight }); };
    source.onerror = () => { if (active) setImageError('图片读取失败，请检查素材访问权限。'); };
    source.src = dataUrl;
    return () => { active = false; source.onload = null; source.onerror = null; dragAbort.current?.abort(); };
  }, [open, dataUrl]);
  useEffect(() => {
    if (!open) return;
    const key = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest("input,textarea,[contenteditable='true']")) return;
      const value = event.key.toLowerCase(), modifier = event.metaKey || event.ctrlKey;
      const isUndo = modifier && !event.altKey && !event.shiftKey && value === 'z';
      const isRedo = modifier && !event.altKey && (event.shiftKey && value === 'z' || !event.shiftKey && value === 'y');
      const isDelete = event.key === 'Delete' || event.key === 'Backspace';
      if (!isUndo && !isRedo && !isDelete) return;
      event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
      if (isDelete) remove(); else if (isRedo) redoChange(); else undo();
    };
    window.addEventListener('keydown', key, true); return () => window.removeEventListener('keydown', key, true);
  }, [open, remove, redoChange, undo]);
  return <Modal title="图片切片" open={open && Boolean(dataUrl)} onCancel={onClose} footer={null} width={780} centered destroyOnHidden maskClosable={!busy} closable={!busy}>
    <div className="space-y-4" data-testid="image-split-dialog" data-canvas-no-zoom>
      <p>按网格生成 {total} 张独立图片，原图继续保留。拖动切线可调整每块范围；按住空格或鼠标中键可平移预览。</p>
      {imageError && <p role="alert">{imageError}</p>}
      <div className="grid gap-5 md:grid-cols-[minmax(260px,1fr)_250px]">
        <div>
          <div ref={viewport.viewportRef} {...viewport.panHandlers} className={`relative h-[340px] rounded-lg bg-black/10 ${viewport.scrollClassName} ${viewport.isPanning ? 'cursor-grabbing' : viewport.spacePressed ? 'cursor-grab' : ''}`}>
            <div className="relative" style={viewport.contentStyle}><div ref={viewport.stageRef} className="absolute overflow-hidden bg-black" data-testid="image-split-preview" style={viewport.stageStyle}>
              <div className="absolute left-0 top-0" style={viewport.mediaStyle}><img src={dataUrl} alt="切片原图" className="block h-full w-full" draggable={false} /></div>
              <div className="pointer-events-none absolute inset-0">{params.verticalLines.map((line, index) => <div key={`v${index}`} data-testid="image-split-vertical-line" className="pointer-events-auto absolute inset-y-0 -ml-2 w-4 cursor-ew-resize" style={{ left: `${line * 100}%` }} onPointerDown={(event) => drag('vertical', index, event)}><div className={`absolute left-1/2 h-full border-l ${active?.axis === 'vertical' && active.index === index ? 'border-amber-300' : 'border-white'}`} /></div>)}{params.horizontalLines.map((line, index) => <div key={`h${index}`} data-testid="image-split-horizontal-line" className="pointer-events-auto absolute inset-x-0 -mt-2 h-4 cursor-ns-resize" style={{ top: `${line * 100}%` }} onPointerDown={(event) => drag('horizontal', index, event)}><div className={`absolute top-1/2 w-full border-t ${active?.axis === 'horizontal' && active.index === index ? 'border-amber-300' : 'border-white'}`} /></div>)}</div>
            </div></div>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1"><Button aria-label="撤销切线" disabled={busy || !history.length} icon={<Undo2 size={15} />} onClick={undo} /><Button aria-label="重做切线" disabled={busy || !redo.length} icon={<Redo2 size={15} />} onClick={redoChange} /><Button aria-label="缩小切片预览" disabled={!viewport.canZoomOut} icon={<ZoomOut size={15} />} onClick={viewport.zoomOut} /><button onClick={viewport.resetZoom}>{Math.round(viewport.zoom * 100)}%</button><Button aria-label="放大切片预览" disabled={!viewport.canZoomIn} icon={<ZoomIn size={15} />} onClick={viewport.zoomIn} /><span className="ml-auto text-xs">{image ? `${image.width} × ${image.height} px` : '正在读取图片'}</span></div>
        </div>
        <div className="space-y-4"><label className="block">行数<InputNumber aria-label="切片行数" data-testid="image-split-rows" className="!w-full" min={1} max={12} precision={0} value={rows} disabled={busy} onChange={(value) => update('rows', value)} /></label><label className="block">列数<InputNumber aria-label="切片列数" data-testid="image-split-columns" className="!w-full" min={1} max={12} precision={0} value={columns} disabled={busy} onChange={(value) => update('columns', value)} /></label>
          <div className="grid grid-cols-2 gap-2"><Button disabled={busy || rows >= 12} icon={<Rows3 size={14} />} onClick={() => add('horizontal')}>添加横线</Button><Button disabled={busy || columns >= 12} icon={<PanelTop size={14} />} onClick={() => add('vertical')}>添加竖线</Button><Button disabled={busy || !active} icon={<Trash2 size={14} />} onClick={remove}>删除切线</Button><Button disabled={busy} icon={<ListRestart size={14} />} onClick={() => { remember(); setActive(null); setParams({ rows, columns, horizontalLines: gridLines(rows), verticalLines: gridLines(columns) }); }}>均分切线</Button></div>
          <p>图片数量：{total}<br />平均尺寸：{image ? `${Math.floor(image.width / columns)} × ${Math.floor(image.height / rows)}` : '正在读取图片'}</p>
          <Button type="primary" data-testid="image-split-confirm" className="w-full" loading={busy} disabled={!image || Boolean(imageError)} icon={<Grid2x2 size={15} />} onClick={() => onConfirm({ ...params, rows, columns })}>生成独立切片</Button>
        </div>
      </div>
    </div>
  </Modal>;
}

export const CanvasNodeSplitDialog = memo(CanvasNodeSplitDialogView);
