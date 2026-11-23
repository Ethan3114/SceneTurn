'use client';

import { useState } from 'react';
import { ArrowDown, ArrowUp, Plus, X } from 'lucide-react';
import type { CanvasNodeData } from '../upstream/canvas';
import { clipFor, downloadMedia, renderMediaEdit, type Caption, type EditSettings, type SourceClip } from './media-edit';
import './production.css';

type Props = { projectId: string; sources: CanvasNodeData[]; settings: EditSettings; audioOnly: boolean; onChange: (value: EditSettings) => void; onSave: (file: File) => Promise<void>; onClose: () => void };

export function MediaCutEditor({ projectId, sources, settings, audioOnly, onChange, onSave, onClose }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [active, setActive] = useState('');
  const [position, setPosition] = useState(0);
  const available = sources.filter(n => n.metadata?.storageKey && (n.type === 'audio' || !audioOnly && n.type === 'video'));
  const ordered = [...new Set([...(settings.order || []), ...available.map(n => n.id)])].flatMap(id => available.filter(n => n.id === id));
  const current = ordered.find(n => n.id === active) || ordered[0];
  const patch = (value: Partial<EditSettings>) => onChange({ ...settings, ...value });
  const updateClip = (n: CanvasNodeData, value: Partial<SourceClip>) => patch({ clips: { ...settings.clips, [n.id]: { ...clipFor(n, settings), ...value } } });
  const move = (index: number, delta: number) => {
    const order = ordered.map(n => n.id);
    [order[index], order[index + delta]] = [order[index + delta], order[index]];
    patch({ order });
  };
  const captions = settings.captions || [];
  const updateCaption = (index: number, value: Partial<Caption>) => patch({ captions: captions.map((item, i) => i === index ? { ...item, ...value } : item) });
  async function output(toCanvas: boolean) {
    setBusy(true); setError('');
    try {
      const file = await renderMediaEdit(projectId, settings, ordered, audioOnly);
      if (toCanvas) await onSave(file); else downloadMedia(file);
    } catch (e) { setError(e instanceof Error ? e.message : '导出失败'); }
    finally { setBusy(false); }
  }
  return <div className="prod-overlay" role="dialog" aria-modal="true" aria-label={audioOnly ? '音频编辑' : '素材混剪'} data-canvas-no-zoom>
    <section className="prod-editor">
      <header><strong>{audioOnly ? '音频编辑' : '素材混剪'}</strong><button aria-label="关闭剪辑" disabled={busy} onClick={onClose}><X size={18}/></button></header>
      <main className="prod-cut-main">
        <div className="prod-monitor">{current?.type === 'video' ? <video key={current.id} src={current.metadata?.content} controls preload="metadata" onTimeUpdate={e => setPosition(e.currentTarget.currentTime)}/> : current ? <audio key={current.id} controls src={current.metadata?.content} onTimeUpdate={e => setPosition(e.currentTarget.currentTime)}/> : <p>请连接来源素材后开始剪辑</p>}</div>
        <div className="prod-tracks">
          {ordered.map((n, i) => { const c = clipFor(n, settings); return <section key={n.id} className="prod-track">
            <div><button aria-pressed={current?.id === n.id} onClick={() => setActive(n.id)}>{n.title}</button><button aria-label="提前片段" disabled={!i} onClick={() => move(i, -1)}><ArrowUp size={14}/></button><button aria-label="推迟片段" disabled={i === ordered.length - 1} onClick={() => move(i, 1)}><ArrowDown size={14}/></button></div>
            <div>{([['start','入点'], ['end','出点'], ['gain','音量'], ['fadeIn','淡入'], ['fadeOut','淡出'], ...(n.type === 'audio' ? [['offset','音轨起点']] : [])] as [keyof Omit<SourceClip,'assetId'>, string][]).map(([key, label]) => <label key={key}>{label}<input aria-label={n.title + label} type="number" min={0} step={key === 'gain' ? 0.1 : 0.01} value={c[key]} onChange={e => updateClip(n,{ [key]: Number(e.target.value) })}/></label>)}</div>
            {current?.id === n.id && <div><button onClick={() => updateClip(n,{ start: +position.toFixed(2) })}>播放头设为入点</button><button onClick={() => updateClip(n,{ end: +position.toFixed(2) })}>播放头设为出点</button></div>}
          </section>; })}
          {!audioOnly && <section className="prod-track"><div><strong>字幕</strong><button onClick={() => patch({ captions: [...captions, {start:0,end:1,text:''}] })}><Plus size={14}/>添加</button></div>
            {captions.map((c,i) => <div key={i}><input aria-label="字幕正文" value={c.text} onChange={e => updateCaption(i,{text:e.target.value})}/><input aria-label="字幕开始" type="number" value={c.start} step=".1" onChange={e => updateCaption(i,{start:Number(e.target.value)})}/><input aria-label="字幕结束" type="number" value={c.end} step=".1" onChange={e => updateCaption(i,{end:Number(e.target.value)})}/><button aria-label="删除字幕" onClick={() => patch({ captions:captions.filter((_,j)=>i!==j) })}><X size={14}/></button></div>)}</section>}
        </div>
      </main>
      <footer>{!audioOnly && <><select aria-label="画幅" value={settings.ratio || '16:9'} onChange={e => patch({ratio:e.target.value as EditSettings['ratio']})}>{['16:9','9:16','1:1'].map(x=><option key={x}>{x}</option>)}</select><select aria-label="导出分辨率" value={settings.resolution || 720} onChange={e=>patch({resolution:Number(e.target.value) as EditSettings['resolution']})}>{[480,720,1080].map(x=><option value={x} key={x}>{x}P</option>)}</select><select aria-label="帧率" value={settings.fps || 30} onChange={e=>patch({fps:Number(e.target.value) as EditSettings['fps']})}>{[24,25,30,60].map(x=><option value={x} key={x}>{x} FPS</option>)}</select><select aria-label="封装格式" value={settings.container || 'mp4'} onChange={e=>patch({container:e.target.value as EditSettings['container']})}><option>mp4</option><option>webm</option></select></>}
        <span role="status">{error || (busy ? '正在导出…' : '')}</span><button disabled={busy || !ordered.length} onClick={()=>void output(false)}>导出到文件</button><button className="prod-primary" disabled={busy || !ordered.length} onClick={()=>void output(true)}>导出到画布</button></footer>
    </section></div>;
}
