'use client';

import { useState } from 'react';
import { AudioLines, Box, ChevronDown, ChevronRight, Film, Folder, Grid2x2, Image as ImageIcon, List, Maximize2, Minimize2, Search, Type, X } from 'lucide-react';
import type { SceneTurnAsset } from '@/app/_types';
import { assetContentUrl } from '@/app/_utils/cloudApi';
import { nodeDramaTask } from './drama-tasks';
import type { CanvasNodeData } from './upstream/canvas';

type Category = 'all' | 'history' | 'uploads' | 'saved';
const mediaLabels = { image: '图片', video: '视频', audio: '音频', text: '文本', group: '分组', director: '导演台', script: '脚本', 'smart-edit': '智能剪辑' };
const mediaIcons = { image: ImageIcon, video: Film, audio: AudioLines, text: Type, group: Folder, director: Box, script: List, 'smart-edit': Film };
type Kind = keyof typeof mediaLabels;
function nodeKind(node: CanvasNodeData): Kind {
  if (node.type === 'config') {
    if (node.metadata?.dramaTask === 'join') return 'smart-edit';
    if (node.metadata?.scriptRows || node.metadata?.dramaTask === 'plot') return 'script';
    return nodeDramaTask(node).kind;
  }
  return Object.hasOwn(mediaLabels, node.type) ? node.type as Kind : 'text';
}

export function AssetThumbnail({ asset }: { asset: SceneTurnAsset }) {
  const Icon = mediaIcons[asset.type];
  if (asset.type === 'image') return <img loading="lazy" src={assetContentUrl(asset.id)} alt="" />;
  if (asset.type === 'video' && typeof asset.metadata?.poster === 'string') return <img loading="lazy" src={asset.metadata.poster} alt="" />;
  return <Icon size={20} aria-hidden="true" />;
}

export function CanvasBrowser({ section, nodes, assets, generated, selectedId, onSection, onFocus, onAsset, onClose }: {
  section: 'nodes' | 'assets'; nodes: CanvasNodeData[]; assets: SceneTurnAsset[]; generated: Set<string>; selectedId: string | null;
  onSection: (section: 'nodes' | 'assets') => void; onFocus: (id: string) => void; onAsset: (asset: SceneTurnAsset) => void; onClose: () => void;
}) {
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  const [grid, setGrid] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [category, setCategory] = useState<Category>('all');
  const [folder, setFolder] = useState('');
  const [closedGroups, setClosedGroups] = useState(new Set<string>());
  const matches = (label: string, type: string) => (kind === 'all' || type === kind) && label.toLowerCase().includes(query.trim().toLowerCase());
  const shownNodes = nodes.filter(node => matches(node.title, nodeKind(node)));
  const folders = [...new Set(assets.map(asset => asset.metadata?.canvasFolder).filter((value): value is string => typeof value === 'string' && Boolean(value)))];
  const byCategory = assets.filter(asset => category === 'all' || (category === 'saved' ? asset.metadata?.canvasSaved === true : category === 'history' ? generated.has(asset.id) : !generated.has(asset.id) && !asset.metadata?.canvasCreated));
  const shownAssets = byCategory.filter(asset => matches(asset.name, asset.type) && (!folder || asset.metadata?.canvasFolder === folder));
  const listed = section === 'nodes' ? shownNodes.length : shownAssets.length;
  const nodeRow = (node: CanvasNodeData) => {
    const Icon = mediaIcons[nodeKind(node)];
    return <button type="button" key={node.id} aria-current={node.id === selectedId} onClick={() => onFocus(node.id)}>
      {node.type === 'image' && node.metadata?.storageKey ? <img loading="lazy" src={assetContentUrl(node.metadata.storageKey)} alt="" /> : <Icon size={17}/>}
      <span>{node.title || mediaLabels[nodeKind(node)]}</span></button>;
  };
  const filtering = Boolean(query.trim() || kind !== 'all');
  return <aside className="fmv-libtv-asset-pane fmv-canvas-browser" aria-label="资产管理" data-expanded={expanded && section === 'assets'} data-canvas-no-zoom>
    <header><strong>{section === 'nodes' ? '节点列表' : '资产库'}</strong><div>{section === 'assets' && <button type="button" aria-label={expanded ? '收回侧栏' : '展开资产管理器'} onClick={() => setExpanded(!expanded)}>{expanded ? <Minimize2 size={15}/> : <Maximize2 size={15}/>}</button>}
      <button type="button" aria-label="收起节点侧栏" onClick={onClose}><X size={16}/></button></div></header>
    <div role="group" aria-label="资产类型">{(['nodes', 'assets'] as const).map(tab => <button key={tab} aria-pressed={section === tab} onClick={() => { onSection(tab); setQuery(''); setKind('all'); }}>{tab === 'nodes' ? '画布' : '资产'}</button>)}</div>
    {section === 'assets' && <div className="fmv-browser-tabs">{([['all','全部'],['history','生成历史'],['uploads','上传'],['saved','已保存']] as const).map(([id,title]) => <button type="button" key={id} aria-pressed={category === id} onClick={() => { setCategory(id); setFolder(''); }}>{title}</button>)}</div>}
    <label><Search size={14}/><input type="search" aria-label={section === 'nodes' ? '搜索节点' : '搜索素材'} placeholder={section === 'nodes' ? '搜索节点' : '搜索素材'} value={query} onChange={event => setQuery(event.target.value)}/></label>
    <div className="fmv-browser-filters"><select aria-label="筛选类型" value={kind} onChange={event => setKind(event.target.value)}><option value="all">全部类型</option>{Object.entries(mediaLabels).filter(([id]) => section === 'nodes' || ['text','image','video','audio'].includes(id)).map(([id,title]) => <option value={id} key={id}>{title}</option>)}</select>
      <button type="button" aria-label={grid ? '列表视图' : '网格视图'} onClick={() => setGrid(!grid)}>{grid ? <List size={16}/> : <Grid2x2 size={16}/>}</button></div>
    {section === 'assets' && folders.length > 0 && <select aria-label="资产文件夹" value={folder} onChange={event => setFolder(event.target.value)}><option value="">全部文件夹</option>{folders.map(name => <option key={name}>{name}</option>)}</select>}
    <div className="fmv-libtv-asset-list" data-grid={grid}>
      {section === 'assets' ? shownAssets.map(asset => <button type="button" key={asset.id} onClick={() => onAsset(asset)}><AssetThumbnail asset={asset}/><span>{asset.name}</span></button>) :
        shownNodes.filter(node => filtering || !node.metadata?.groupId || !nodes.some(group => group.id === node.metadata?.groupId)).map(node => {
          if (node.type !== 'group' || filtering) return nodeRow(node);
          const closed = closedGroups.has(node.id);
          return <section className="fmv-browser-group" key={node.id}><header><button type="button" aria-label={closed ? '展开分组' : '收起分组'} aria-expanded={!closed} onClick={() => setClosedGroups(current => { const next = new Set(current); if (closed) next.delete(node.id); else next.add(node.id); return next; })}>{closed ? <ChevronRight size={14}/> : <ChevronDown size={14}/>}</button>{nodeRow(node)}</header>
            {!closed && nodes.filter(child => child.metadata?.groupId === node.id).map(nodeRow)}</section>;
        })}
      {!listed && <p role="status">{query || kind !== 'all' ? '没有符合条件的内容' : section === 'nodes' ? '画布中还没有节点' : '这个分类还没有素材'}</p>}
    </div>
    <footer>共 {listed} {section === 'nodes' ? '个节点' : '项素材'}</footer>
  </aside>;
}
