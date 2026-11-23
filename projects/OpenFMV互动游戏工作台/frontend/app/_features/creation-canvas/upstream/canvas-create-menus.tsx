import { useEffect, useRef, useState, type RefObject } from 'react';
import { AudioLines, ChevronRight, Clapperboard, History, ImageIcon, NotebookText, Scissors, Type, Upload, Video, type LucideIcon } from 'lucide-react';

import type { DramaTaskId } from '../drama-tasks';
import type { ConnectionHandle, Position } from './canvas';
import './canvas-create-menus.css';

export type LinkedGenerationKind = 'text' | 'image' | 'video' | 'frames' | 'reference' | 'edit' | 'extend' | 'join' | 'audio' | 'script';
export type LinkedGenerationOption = {
  kind: LinkedGenerationKind;
  task: DramaTaskId;
  modelProfileId?: string;
  disabled?: boolean;
};

export type PendingConnectionCreate = {
  connection: ConnectionHandle;
  sourceIds?: string[];
  position: Position;
  menuPosition: Position;
  options: LinkedGenerationOption[];
  title?: string;
};

export type CanvasCreateKind = 'text' | 'script' | 'image' | 'video' | 'director' | 'smart-edit' | 'audio';
type CreateSettings = { legacyScript?: boolean };
type Badge = 'NEW' | 'Beta';

const creationRows: { kind: CanvasCreateKind; title: string; icon: LucideIcon; badge?: Badge }[] = [
  { kind: 'text', title: '文本', icon: Type },
  { kind: 'script', title: '脚本', icon: NotebookText },
  { kind: 'image', title: '图片', icon: ImageIcon },
  { kind: 'video', title: '视频', icon: Video },
  { kind: 'director', title: '导演台', icon: Clapperboard, badge: 'NEW' },
  { kind: 'smart-edit', title: '智能剪辑', icon: Scissors, badge: 'Beta' },
  { kind: 'audio', title: '音频', icon: AudioLines },
];

const connectionRows: { kinds: LinkedGenerationKind[]; title: string; icon: LucideIcon }[] = [
  { kinds: ['text'], title: '文本生成', icon: Type },
  { kinds: ['image'], title: '图片生成', icon: ImageIcon },
  { kinds: ['video', 'reference', 'frames', 'edit', 'extend'], title: '视频生成', icon: Video },
  { kinds: ['join'], title: '智能剪辑', icon: Scissors },
  { kinds: ['audio'], title: '音频', icon: AudioLines },
  { kinds: ['script'], title: '脚本生成', icon: NotebookText },
];

function useMenuDismiss({
  menuRef,
  onClose,
  trigger,
}: {
  menuRef: RefObject<HTMLDivElement | null>;
  onClose: () => void;
  trigger?: string;
}) {
  useEffect(() => {
    const dismissOutside = (event: PointerEvent) => {
      if (trigger && event.target instanceof Element && event.target.closest(trigger)) return;
      if (event.target instanceof Node && !menuRef.current?.contains(event.target)) onClose();
    };
    const dismissWithEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) onClose();
    };
    document.addEventListener('pointerdown', dismissOutside, true);
    document.addEventListener('keydown', dismissWithEscape);
    return () => {
      document.removeEventListener('pointerdown', dismissOutside, true);
      document.removeEventListener('keydown', dismissWithEscape);
    };
  }, [menuRef, onClose, trigger]);
}

type MenuRowProps = {
  icon: LucideIcon;
  title: string;
  badge?: Badge;
  disabled?: boolean;
  disabledReason?: string;
  testId?: string;
  submenuOpen?: boolean;
  onClick?: () => void;
};

function MenuRow({ icon: Icon, title, badge, disabled, disabledReason, testId, submenuOpen, onClick }: MenuRowProps) {
  return (
    <button
      type="button"
      className="fmv-libtv-create-row"
      data-testid={testId}
      disabled={disabled}
      title={disabledReason}
      aria-haspopup={submenuOpen !== undefined ? 'menu' : undefined}
      aria-expanded={submenuOpen}
      onClick={onClick}
    >
      <Icon size={16} strokeWidth={1.5} aria-hidden="true" />
      <span>{title}</span>
      {badge && <span data-badge={badge} className="fmv-libtv-create-badge">{badge}</span>}
      {submenuOpen !== undefined && <ChevronRight size={14} aria-hidden="true" className="fmv-libtv-create-submenu-icon" />}
    </button>
  );
}

export function ConnectionCreateMenu({ pending, scale = 1, onCreate, onClose }: {
  pending: PendingConnectionCreate;
  scale?: number;
  onCreate: (option: LinkedGenerationOption) => void;
  onClose: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const title = pending.title || '引用当前节点';
  useMenuDismiss({ menuRef, onClose });
  useEffect(() => {
    menuRef.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({ preventScroll: true });
  }, []);

  return (
    <div
      ref={menuRef}
      className="fmv-libtv-create-menu"
      data-connection-create-menu
      data-canvas-no-zoom
      data-testid="canvas-connection-create-menu"
      role="group"
      aria-label={title}
      style={{ left: pending.menuPosition.x, top: pending.menuPosition.y, transform: `scale(${1 / scale})` }}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
    >
      <div className="fmv-libtv-create-heading">{title}</div>
      {connectionRows.map(({ kinds, title, icon }) => {
        const option = kinds.map((kind) => pending.options.find((entry) => entry.kind === kind)).find((entry) => entry && !entry.disabled);
        return <MenuRow key={kinds[0]} icon={icon} title={title} testId={`canvas-linked-create-${kinds[0]}`} disabled={!option} onClick={() => { if (option) onCreate(option); }} />;
      })}
    </div>
  );
}

export function NodeCreateMenu({ position, screenAnchor, scale = 1, onCreate, onClose, onUpload, onHistory }: {
  position: Position;
  screenAnchor?: Position;
  scale?: number;
  onCreate: (type: CanvasCreateKind, settings?: CreateSettings) => void;
  onClose: () => void;
  onUpload?: () => void;
  onHistory?: () => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const scriptMenuRef = useRef<HTMLDivElement>(null);
  const [scriptOpen, setScriptOpen] = useState(false);
  const anchor = screenAnchor ?? position;
  useMenuDismiss({ menuRef, onClose, trigger: screenAnchor ? '[data-testid="canvas-add-node"]' : undefined });

  return (
    <div
      ref={menuRef}
      className="fmv-libtv-create-menu"
      data-canvas-no-zoom
      data-testid="canvas-node-create-menu"
      role="group"
      aria-label="新建节点"
      style={{ left: anchor.x, top: anchor.y, transform: screenAnchor ? undefined : `scale(${1 / scale})` }}
      onMouseDown={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
    >
      {creationRows.map(({ kind, title, icon, badge }) => {
        if (kind === 'script') {
          return (
            <div
              key={kind}
              className="fmv-libtv-create-script"
              onMouseEnter={() => setScriptOpen(true)}
              onMouseLeave={() => setScriptOpen(false)}
              onKeyDown={(event) => {
                if (event.key === 'ArrowRight') {
                  event.preventDefault();
                  setScriptOpen(true);
                  requestAnimationFrame(() => scriptMenuRef.current?.querySelector('button')?.focus());
                }
                if (scriptOpen && (event.key === 'ArrowLeft' || event.key === 'Escape')) {
                  event.preventDefault();
                  setScriptOpen(false);
                  event.currentTarget.querySelector('button')?.focus();
                }
              }}
            >
              <MenuRow icon={icon} title={title} submenuOpen={scriptOpen} testId="canvas-create-script-menu" onClick={() => setScriptOpen(true)} />
              {scriptOpen && (
                <div className="fmv-libtv-create-script-flyout">
                  <div className="fmv-libtv-create-menu fmv-libtv-create-script-menu" ref={scriptMenuRef} role="group" aria-label="脚本类型">
                    <MenuRow icon={icon} title="脚本" badge="NEW" testId="canvas-create-script" onClick={() => onCreate('script')} />
                    <MenuRow icon={icon} title="脚本（旧版）" badge="Beta" testId="canvas-create-script-legacy" onClick={() => onCreate('script', { legacyScript: true })} />
                  </div>
                </div>
              )}
            </div>
          );
        }
        return <MenuRow key={kind} icon={icon} title={title} badge={badge} testId={`canvas-create-${kind}`} onClick={() => onCreate(kind)} />;
      })}
      <div className="fmv-libtv-create-resource" role="group" aria-label="添加资源">
        <div className="fmv-libtv-create-heading">添加资源</div>
        <MenuRow icon={Upload} title="上传" testId="canvas-create-upload" onClick={onUpload} disabled={!onUpload} />
        <MenuRow icon={History} title="从生成历史选择" testId="canvas-create-history" onClick={onHistory} disabled={!onHistory} />
      </div>
    </div>
  );
}
