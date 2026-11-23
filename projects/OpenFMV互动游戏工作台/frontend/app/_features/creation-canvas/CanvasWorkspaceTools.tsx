'use client';

import { useState, type FormEvent } from 'react';
import { AudioLines, Box, Clapperboard, Film, Image as ImageIcon, NotebookText, Scissors, Search, Type, UserRound } from 'lucide-react';
import type { OpenFMVAsset } from '@/app/_types';
import type { CanvasCreateKind } from './upstream/canvas-create-menus';
import { AssetThumbnail } from './CanvasBrowser';

export const toolDirectory = [
  { kind: 'text', name: '文本', icon: Type }, { kind: 'script', name: '脚本', icon: NotebookText },
  { kind: 'image', name: '图片生成', icon: ImageIcon }, { kind: 'video', name: '视频生成', icon: Film },
  { kind: 'audio', name: '音频', icon: AudioLines }, { kind: 'director', name: '导演台', icon: Clapperboard },
  { kind: 'smart-edit', name: '智能剪辑', icon: Scissors }, { kind: 'character', name: '角色造型室', icon: UserRound },
  { kind: 'split', name: '图片切片', icon: Box },
] as const;
export type DirectoryAction = typeof toolDirectory[number]['kind'];
const resultTools = [
  ...['高清放大', '扩图', '局部重绘', '擦除', '裁切', '重新打光', '变化机位'].map(name => ({ name, type: 'image' as const })),
  ...['提取首帧', '提取当前帧', '提取尾帧', '截取片段', '片段重拍', '视频高清', '画面编辑', '提取音轨', '静音视频', '去字幕'].map(name => ({ name, type: 'video' as const })),
  { name: '编辑音频', type: 'audio' as const },
];
type ToolSource = typeof resultTools[number];
export function CanvasToolDirectory({ onChoose, assets, onResultTool }: { onChoose: (kind: CanvasCreateKind | 'character') => void; assets: OpenFMVAsset[]; onResultTool: (asset: OpenFMVAsset, name: string) => void }) {
  const [search, setSearch] = useState('');
  const [picker, setPicker] = useState<ToolSource | null>(null);
  const entries = toolDirectory.filter(item => item.name.includes(search.trim()));
  const operations = resultTools.filter(item => item.name.includes(search.trim()));
  if (picker) {
    const candidates = assets.filter(a => a.type === picker.type);
    const mediaName = { image: '图片', video: '视频', audio: '音频' }[picker.type];
    return <div className="fmv-tool-directory"><button onClick={() => setPicker(null)}>‹ 全部功能</button><p>{picker.name} · 选择{mediaName}</p>
      <div className="fmv-libtv-library">{candidates.map(asset => <button key={asset.id} onClick={() => onResultTool(asset, picker.name)}><AssetThumbnail asset={asset}/>{asset.name}</button>)}</div>
      {!candidates.length && <p>请先从添加菜单上传{mediaName}。</p>}</div>;
  }
  return <div className="fmv-tool-directory"><label><Search size={16}/><input aria-label="搜索工具" type="search" value={search} placeholder="搜索工具" onChange={e => setSearch(e.target.value)}/></label>
    <div>{entries.map(({ kind, name, icon: Icon }) => <button type="button" key={kind} onClick={() => { if (kind === 'split') setPicker({name, type:'image'}); else onChoose(kind); }}><Icon size={18}/>{name}</button>)}
      {operations.map(item => <button type="button" key={item.name} onClick={() => setPicker(item)}>{item.name}</button>)}</div>
    {!entries.length && !operations.length && <p>没有找到工具</p>}</div>;
}

export type CharacterForm = { name: string; face: string; body: string; outfit: string; notes: string; reference: string; authorized: boolean };
export const emptyCharacterForm: CharacterForm = { name: '', face: '', body: '', outfit: '', notes: '', reference: '', authorized: false };
export function CharacterStudio({ initial, assets, onApply, onImport }: {
  initial: CharacterForm; assets: OpenFMVAsset[]; onApply: (values: CharacterForm) => void; onImport: (file: File) => Promise<OpenFMVAsset>;
}) {
  const [value, setValue] = useState(initial);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [imported, setImported] = useState<OpenFMVAsset[]>([]);
  const photos = [...new Map([...assets, ...imported].filter(a => a.type === 'image').map(a => [a.id, a])).values()];
  const fields = [['name','角色名称'],['face','脸部与发型'],['body','身材与姿态'],['outfit','服装'],['notes','补充说明']] as const;
  function submit(event: FormEvent) { event.preventDefault(); if (value.name.trim() && (value.reference || value.face.trim() || value.body.trim() || value.outfit.trim() || value.notes.trim())) onApply(value); }
  return <form className="fmv-canvas-form" onSubmit={submit}>
    {fields.map(([key, label]) => <label key={key}>{label}<input maxLength={key === 'name' ? 120 : 1200} required={key === 'name'} value={value[key]} onChange={event => setValue({ ...value, [key]: event.target.value })}/></label>)}
    <label>角色参考图<select value={value.reference} onChange={event => setValue({ ...value, reference: event.target.value, authorized: false })}><option value="">从文字描述创建</option>{photos.map(asset => <option value={asset.id} key={asset.id}>{asset.name}</option>)}</select></label>
    <label>上传参考图<input type="file" accept="image/*" disabled={uploading} onChange={async event => {
      const file = event.target.files?.[0]; if (!file) return; setUploading(true); setError('');
      try { const asset = await onImport(file); setImported(old => [...old, asset]); setValue(old => ({ ...old, reference: asset.id, authorized: false })); }
      catch (problem) { setError(problem instanceof Error ? problem.message : '图片上传失败'); }
      finally { setUploading(false); }
    }}/></label>
    {value.reference && <label className="fmv-inline-check"><input type="checkbox" required checked={value.authorized} onChange={event => setValue({ ...value, authorized: event.target.checked })}/>我已获得该形象的使用授权</label>}
    {error && <p role="alert">{error}</p>}
    <button type="submit" disabled={uploading || !value.name.trim() || ![value.reference,value.face,value.body,value.outfit,value.notes].some(x=>x.trim()) || Boolean(value.reference && !value.authorized)}>添加到画布</button>
  </form>;
}

export const canvasKeyGroups = [
  { name: '创作', items: [['打开节点编辑','单击节点'],['多选节点','Shift + 单击'],['框选节点','Shift + 拖动空白处'],['复制选中节点','⌘ / Ctrl + C'],['粘贴节点','⌘ / Ctrl + V'],['创建带连线的副本','⌘ / Ctrl + D']] },
  { name: '缩放', items: [['放大','+'],['缩小','−'],['适配画面','1'],['实际大小','0']] },
  { name: '移动', items: [['平移视野','直接拖动'],['移动节点','长按节点后拖动'],['临时抓手','空格 + 拖动'],['平移视野','鼠标中键 + 拖动']] },
  { name: '其他', items: [['关闭浮层','Esc'],['查看快捷键','?']] },
];
export function CanvasShortcutTable() {
  return <div className="fmv-shortcuts">{canvasKeyGroups.map(group => <section key={group.name}><h3>{group.name}</h3>{group.items.map(([name,key]) => <div key={name}><span>{name}</span><kbd>{key}</kbd></div>)}</section>)}</div>;
}
