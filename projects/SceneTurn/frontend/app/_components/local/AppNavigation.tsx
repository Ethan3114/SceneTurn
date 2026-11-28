'use client';

import React, { useSyncExternalStore } from 'react';
import Link from 'next/link';
import { useLocale, useTranslations } from 'next-intl';
import { Minus, Settings, Sparkles, Square, X } from 'lucide-react';

import { getLocalizedPath } from '@/app/_utils/localePaths';
import SceneTurnMark from '@/app/_components/workbench/SceneTurnMark';
import { useProjectSessionStore } from '@/app/_features/project-session/store';

const subscribeToNativeBridge = () => () => undefined;
const hasNativeBridge = () => Boolean(window.sceneturn);
const serverHasNativeBridge = () => false;
export const useNativeWindow = () => useSyncExternalStore(subscribeToNativeBridge, hasNativeBridge, serverHasNativeBridge);

interface AppNavigationProps {
  chatOpen?: boolean;
  compact?: boolean;
  windowControlsOnly?: boolean;
  onOpenSettings?: () => void;
  onToggleChat?: () => void;
}

export default function AppNavigation({ chatOpen = false, compact = false, windowControlsOnly = false, onOpenSettings, onToggleChat }: AppNavigationProps) {
  const locale = useLocale();
  const t = useTranslations('navigation');
  const settingsT = useTranslations('settings');
  const nativeWindow = useNativeWindow();
  const projectId = useProjectSessionStore((state) => state.projectId);
  const modelsPath = getLocalizedPath(locale, '/settings/models');
  const settingsPath = projectId ? `${modelsPath}?projectId=${encodeURIComponent(projectId)}` : modelsPath;

  return (
    <header className={`relative z-[100] flex shrink-0 items-center bg-[#181818] [-webkit-app-region:drag] ${windowControlsOnly ? 'justify-end' : 'justify-between'} ${compact ? 'h-10 px-3' : 'h-14 px-4 sm:px-7'}`}>
      {!windowControlsOnly && <Link href={getLocalizedPath(locale, '/projects')} className="flex min-w-0 items-center gap-3 text-white/70 transition hover:text-white [-webkit-app-region:no-drag]">
        <SceneTurnMark size={compact ? 22 : 26} className="shrink-0" />
        <span className={`truncate font-semibold ${compact ? 'text-sm' : 'text-base'}`}>SceneTurn</span>
      </Link>}

      <div className={`flex min-w-0 items-center [-webkit-app-region:no-drag] ${compact ? 'gap-2' : 'gap-3'}`}>
        {!windowControlsOnly && <><button type="button" onClick={onToggleChat} className={`inline-flex items-center gap-2 rounded-[10px] font-semibold transition ${compact ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-sm'} ${chatOpen ? 'bg-white/[0.17] text-white' : 'bg-white/[0.09] text-white/86 hover:bg-white/[0.14] hover:text-white'}`} title={t('askAi')}>
          <Sparkles size={compact ? 14 : 16} className="text-cyan-300" />
          <span>{t('askAi')}</span>
        </button>
        <button type="button" onClick={() => { if (nativeWindow) onOpenSettings?.(); else window.location.assign(settingsPath); }} className={`flex items-center justify-center rounded-[10px] text-white/75 transition hover:bg-white/[0.08] hover:text-white ${compact ? 'h-7 w-7' : 'h-8 w-8'}`} title={settingsT('open')}>
          <Settings size={compact ? 14 : 16} />
        </button></>}
        {nativeWindow && <>
        {!windowControlsOnly && <span aria-hidden="true" className={`hidden select-none leading-none text-white/30 md:block ${compact ? 'text-base' : 'text-lg'}`}>|</span>}
        <button type="button" onClick={() => void window.sceneturn?.minimizeWindow?.()} className={`hidden items-center justify-center text-white/75 transition hover:text-white md:flex ${compact ? 'h-7 w-7' : 'h-8 w-8'}`} title={t('minimize')}>
          <Minus size={17} />
        </button>
        <button type="button" onClick={() => void window.sceneturn?.toggleMaximizeWindow?.()} className={`hidden items-center justify-center text-white/75 transition hover:text-white md:flex ${compact ? 'h-7 w-7' : 'h-8 w-8'}`} title={t('maximize')}>
          <Square size={14} />
        </button>
        <button type="button" onClick={() => void window.sceneturn?.closeWindow?.()} className={`hidden items-center justify-center text-white/75 transition hover:bg-red-500 hover:text-white md:flex ${compact ? 'h-7 w-7' : 'h-8 w-8'}`} title={t('close')}>
          <X size={17} />
        </button>
        </>}
      </div>
    </header>
  );
}
