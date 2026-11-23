'use client';

import { AlignLeft, AudioLines, FileText, Image as ImageIcon, Video } from 'lucide-react';

import './canvas-script-table.css';

export type TextShortcut = 'write' | 'video' | 'reverse' | 'music';

const shortcuts = [
  { action: 'write', label: '自己编写内容', icon: FileText },
  { action: 'video', label: '文生视频', icon: Video },
  { action: 'reverse', label: '图片反推提示词', icon: ImageIcon },
  { action: 'music', label: '文字生音乐', icon: AudioLines },
] as const;

export function CanvasTextShortcuts({ onChoose }: { onChoose: (action: TextShortcut) => void }) {
  return <div className="fmv-text-shortcuts" data-canvas-no-zoom onMouseDown={(event) => event.stopPropagation()} onDoubleClick={(event) => {
    if (event.target instanceof Element && !event.target.closest('button')) { event.stopPropagation(); onChoose('write'); }
  }}>
    <AlignLeft className="fmv-text-shortcuts-symbol" aria-hidden="true" />
    <div className="fmv-text-shortcuts-actions">
      <span>尝试：</span>
      {shortcuts.map(({ action, label, icon: Icon }) => <button type="button" key={action} onClick={() => onChoose(action)}><Icon aria-hidden="true" />{label}</button>)}
    </div>
  </div>;
}
