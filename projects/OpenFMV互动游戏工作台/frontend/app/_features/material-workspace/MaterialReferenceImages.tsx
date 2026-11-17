'use client';

import { useId, useState } from 'react';

import { ImagePlus, X } from 'lucide-react';

import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { assetContentUrl } from '@/app/_utils/cloudApi';
import type { MaterialReferenceAsset } from '@/app/_utils/materialApi';

interface MaterialReferenceImagesProps {
  projectId: string;
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  frozenAssets?: MaterialReferenceAsset[];
  disabled?: boolean;
  actionLabel?: string;
}

export function requireProjectReferenceImages(projectId: string, ids: string[]) {
  if (!ids.length) return;
  const session = useProjectSessionStore.getState();
  const availableIds = new Set(session.projectId === projectId ? session.assets.filter((asset) => asset.type === 'image').map((asset) => asset.id) : []);
  if (ids.some((id) => !availableIds.has(id))) throw new Error('选中的参考图片已不可用，请移除后重新选择。');
}

function ReferenceImageThumbnail({ assetId, name }: { assetId: string; name: string }) {
  const src = assetContentUrl(assetId);
  return <a href={src} target="_blank" rel="noopener noreferrer" aria-label={`查看参考图片 ${name}（新标签页）`} className="inline-flex shrink-0 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-orange-500"><img src={src} alt="" loading="lazy" width={64} height={48} className="h-12 w-16 rounded bg-black/30 object-contain" /></a>;
}

export default function MaterialReferenceImages({ projectId, selectedIds, onChange, frozenAssets, disabled = false, actionLabel = '添加参考图片' }: MaterialReferenceImagesProps) {
  const sessionProjectId = useProjectSessionStore((state) => state.projectId);
  const assets = useProjectSessionStore((state) => state.assets);
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const images = sessionProjectId === projectId ? assets.filter((asset) => asset.type === 'image') : [];
  const frozen = frozenAssets || [];
  const frozenIds = new Set(frozen.map((asset) => asset.assetId));
  const pendingIds = selectedIds.filter((id) => !frozenIds.has(id));
  const count = frozenIds.size + pendingIds.length;

  function toggle(id: string) {
    if (disabled || frozenIds.has(id)) return;
    if (pendingIds.includes(id)) onChange(pendingIds.filter((item) => item !== id));
    else if (count < 5) onChange([...pendingIds, id]);
  }

  return <div className="my-2 space-y-2" data-testid="material-reference-images">
    {frozen.length > 0 && <div><p className="material-subtle">本轮已引用的图片</p><div className="material-reference-list">{frozen.map((asset) => <span key={asset.assetId} className="max-w-full"><ReferenceImageThumbnail assetId={asset.assetId} name={asset.name} /><span className="min-w-0 break-all">{asset.name} · 已引用</span></span>)}</div></div>}
    {pendingIds.length > 0 && <div className="material-reference-list">{pendingIds.map((id) => {
      const asset = images.find((image) => image.id === id);
      const name = asset?.name || '图片已不可用';
      return <span key={id} className="max-w-full">{asset && <ReferenceImageThumbnail assetId={id} name={name} />}<span className="min-w-0 break-all">{name}</span><button type="button" disabled={disabled} aria-label={`移除参考图片 ${name}`} onClick={() => toggle(id)}><X size={12} /></button></span>;
    })}</div>}
    <button type="button" aria-expanded={expanded} aria-controls={listId} disabled={disabled} onClick={() => setExpanded((value) => !value)}><ImagePlus size={15} />{expanded ? '收起图片选择' : actionLabel}<span className="material-subtle">{count}/5</span></button>
    {expanded && <div id={listId} className="max-h-48 overflow-auto rounded-lg border border-[#49453d] p-2" aria-label="项目参考图片">
      {images.length === 0 ? <p className="material-subtle">项目中还没有图片，请在“项目素材”中导入图片。</p> : <fieldset disabled={disabled}><legend className="material-subtle">选择项目图片，最多五张</legend>{images.map((image) => {
        const isFrozen = frozenIds.has(image.id);
        const checked = isFrozen || pendingIds.includes(image.id);
        return <label key={image.id} className="!my-1 !flex items-center gap-2 rounded p-2 hover:bg-white/5"><input type="checkbox" checked={checked} disabled={isFrozen || (!checked && count >= 5)} onChange={() => toggle(image.id)} /><ReferenceImageThumbnail assetId={image.id} name={image.name} /><span className="min-w-0 break-all">{image.name}</span>{isFrozen && <span className="material-subtle shrink-0">已引用</span>}</label>;
      })}</fieldset>}
      {count >= 5 && <p className="material-subtle" role="status">已选择五张参考图片。</p>}
    </div>}
  </div>;
}
