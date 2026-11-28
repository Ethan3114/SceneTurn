'use client';

import React, { createContext, PointerEvent, Suspense, useEffect, useRef, useState } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';

import AppNavigation, { useNativeWindow } from './AppNavigation';
import ProjectCodexChat from './ProjectCodexChat';
import MaterialChat from '@/app/_features/material-workspace/MaterialChat';
import { useMaterialWorkspace } from '@/app/_features/material-workspace/store';
import SceneTurnAiSettingsCenter from './SceneTurnAiSettingsCenter';
import { stripLocaleFromPath } from '@/app/_utils/localePaths';
import { isAssetWorkspaceOpen, useEditorStore } from '@/app/_store/useEditorStore';
import { usePlayerStore } from '@/app/_store/usePlayerStore';

type ResizeDirection = 'top' | 'right' | 'bottom' | 'left' | 'top-left' | 'top-right' | 'bottom-right' | 'bottom-left';

export const ProjectChatControl = createContext<{ open: boolean; toggle: () => void } | null>(null);

function OpenRequestedCodexChat({ onOpen }: { onOpen: (open: boolean) => void }) {
  const params = useSearchParams();
  useEffect(() => {
    if (params.get('id') && params.get('connectCodex') === '1') onOpen(true);
  }, [onOpen, params]);
  return null;
}

export default function AppWindowFrame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const pathWithoutLocale = stripLocaleFromPath(pathname || '/');
  const [showChat, setShowChat] = useState(false);
  const materialScope = useMaterialWorkspace((state) => state.scope);
  const materialChatOpen = useMaterialWorkspace((state) => state.chatOpen);
  const assetWorkspaceOpen = useEditorStore(isAssetWorkspaceOpen) && pathWithoutLocale === '/editor';
  const isPreviewPlaying = usePlayerStore((state) => state.isPlaying);
  const nativeWindow = useNativeWindow();
  const usesProjectNavigation = pathWithoutLocale === '/editor' || pathWithoutLocale === '/nodes';
  const showApplicationHeader = nativeWindow || !usesProjectNavigation;
  const usesStandaloneMaterials = Boolean(materialScope && !usesProjectNavigation);
  const chatVisible = usesStandaloneMaterials ? materialChatOpen : showChat;
  const projectChatControl = { open: showChat, toggle: () => setShowChat((current) => !current) };
  const [showSettings, setShowSettings] = useState(false);
  const [chatFloating, setChatFloating] = useState(false);
  const [chatPosition, setChatPosition] = useState({ x: 620, y: usesProjectNavigation ? 76 : 16 });
  const [chatSize, setChatSize] = useState({ width: 420, height: 760 });
  const dragStateRef = useRef<{ pointerId: number; startX: number; startY: number; originX: number; originY: number } | null>(null);
  const resizeStateRef = useRef<{ pointerId: number; direction: ResizeDirection; startX: number; startY: number; originX: number; originY: number; originWidth: number; originHeight: number } | null>(null);

  const toggleChat = () => {
    if (usesStandaloneMaterials) useMaterialWorkspace.setState({ chatOpen: !materialChatOpen });
    else setShowChat((current) => !current);
  };

  const openSettings = () => {
    if (window.sceneturn) setShowSettings(true);
    else window.location.assign('/zh-CN/settings/wan');
  };

  const closeChat = () => {
    if (usesStandaloneMaterials) useMaterialWorkspace.setState({ chatOpen: false });
    setShowChat(false);
    setChatFloating(false);
  };

  const toggleChatFloating = () => {
    if (!chatFloating && usesProjectNavigation && chatPosition.y < 76) {
      setChatPosition({ ...chatPosition, y: 76 });
    }
    setChatFloating((current) => !current);
  };

  const startChatDrag = (event: PointerEvent<HTMLElement>) => {
    if (!chatFloating || event.button !== 0) return;
    const target = event.target as HTMLElement;
    if (target.closest('[data-chat-resize-handle]')) return;
    if (!target.closest('[data-chat-drag-handle]') || target.closest('button')) return;
    dragStateRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: chatPosition.x,
      originY: chatPosition.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const startChatResize = (event: PointerEvent<HTMLElement>) => {
    if (!chatFloating || event.button !== 0) return;
    const target = event.target as HTMLElement;
    const handle = target.closest('[data-chat-resize-handle]') as HTMLElement | null;
    const direction = handle?.dataset.chatResizeHandle as ResizeDirection | undefined;
    if (!direction) return;
    resizeStateRef.current = {
      pointerId: event.pointerId,
      direction,
      startX: event.clientX,
      startY: event.clientY,
      originX: chatPosition.x,
      originY: chatPosition.y,
      originWidth: chatSize.width,
      originHeight: chatSize.height,
    };
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const moveChatDrag = (event: PointerEvent<HTMLElement>) => {
    const dragState = dragStateRef.current;
    if (!dragState || dragState.pointerId !== event.pointerId) return;
    setChatPosition({
      x: Math.max(8, dragState.originX + event.clientX - dragState.startX),
      y: Math.max(8, dragState.originY + event.clientY - dragState.startY),
    });
  };

  const moveChatResize = (event: PointerEvent<HTMLElement>) => {
    const resizeState = resizeStateRef.current;
    if (!resizeState || resizeState.pointerId !== event.pointerId) return;
    const minWidth = 340;
    const minHeight = 520;
    const edgeInset = 8;
    const deltaX = event.clientX - resizeState.startX;
    const deltaY = event.clientY - resizeState.startY;
    const canResizeLeft = resizeState.direction.includes('left');
    const canResizeRight = resizeState.direction.includes('right');
    const canResizeTop = resizeState.direction.includes('top');
    const canResizeBottom = resizeState.direction.includes('bottom');
    let nextX = resizeState.originX;
    let nextY = resizeState.originY;
    let nextWidth = resizeState.originWidth;
    let nextHeight = resizeState.originHeight;

    if (canResizeRight) {
      nextWidth = resizeState.originWidth + deltaX;
    }
    if (canResizeLeft) {
      nextX = resizeState.originX + deltaX;
      nextWidth = resizeState.originWidth - deltaX;
      if (nextX < edgeInset) {
        nextWidth += nextX - edgeInset;
        nextX = edgeInset;
      }
    }
    if (canResizeBottom) {
      nextHeight = resizeState.originHeight + deltaY;
    }
    if (canResizeTop) {
      nextY = resizeState.originY + deltaY;
      nextHeight = resizeState.originHeight - deltaY;
      if (nextY < edgeInset) {
        nextHeight += nextY - edgeInset;
        nextY = edgeInset;
      }
    }

    nextWidth = Math.min(Math.max(minWidth, nextWidth), Math.max(minWidth, window.innerWidth - nextX - edgeInset));
    nextHeight = Math.min(Math.max(minHeight, nextHeight), Math.max(minHeight, window.innerHeight - nextY - edgeInset));
    if (canResizeLeft && nextWidth === minWidth) nextX = resizeState.originX + resizeState.originWidth - minWidth;
    if (canResizeTop && nextHeight === minHeight) nextY = resizeState.originY + resizeState.originHeight - minHeight;

    setChatPosition({ x: Math.max(edgeInset, nextX), y: Math.max(edgeInset, nextY) });
    setChatSize({ width: nextWidth, height: nextHeight });
  };

  const stopChatDrag = (event: PointerEvent<HTMLElement>) => {
    if (dragStateRef.current?.pointerId === event.pointerId) {
      dragStateRef.current = null;
    }
    if (resizeStateRef.current?.pointerId === event.pointerId) {
      resizeStateRef.current = null;
    }
  };

  const chatResizeHandles = chatFloating ? (
    <>
      <div data-chat-resize-handle="top" className="absolute left-4 right-4 top-0 z-20 h-2 cursor-ns-resize" />
      <div data-chat-resize-handle="right" className="absolute bottom-4 right-0 top-4 z-20 w-2 cursor-ew-resize" />
      <div data-chat-resize-handle="bottom" className="absolute bottom-0 left-4 right-4 z-20 h-2 cursor-ns-resize" />
      <div data-chat-resize-handle="left" className="absolute bottom-4 left-0 top-4 z-20 w-2 cursor-ew-resize" />
      <div data-chat-resize-handle="top-left" className="absolute left-0 top-0 z-20 h-5 w-5 cursor-nwse-resize rounded-br-[10px]" />
      <div data-chat-resize-handle="top-right" className="absolute right-0 top-0 z-20 h-5 w-5 cursor-nesw-resize rounded-bl-[10px]" />
      <div data-chat-resize-handle="bottom-right" className="absolute bottom-0 right-0 z-20 h-5 w-5 cursor-nwse-resize rounded-tl-[10px] border-l border-t border-white/10 bg-white/[0.06]" />
      <div data-chat-resize-handle="bottom-left" className="absolute bottom-0 left-0 z-20 h-5 w-5 cursor-nesw-resize rounded-tr-[10px]" />
    </>
  ) : null;

  if (['/', '/works', '/account', '/privacy', '/terms'].includes(pathWithoutLocale) || pathWithoutLocale.startsWith('/works/')) {
    return <>{children}</>;
  }

  if (!nativeWindow && (pathWithoutLocale === '/projects' || pathWithoutLocale.startsWith('/settings/'))) {
    return <>{children}</>;
  }

  if (pathWithoutLocale.startsWith('/asset-studio')) {
    return (
      <div className="relative h-[100dvh] overflow-hidden bg-[#111] text-sceneturn-text">
        <AppNavigation chatOpen={chatVisible} onOpenSettings={openSettings} onToggleChat={toggleChat} />
        <div className="h-[calc(100dvh-3.5rem)] overflow-hidden">{children}</div>
        {showSettings && <SceneTurnAiSettingsCenter onClose={() => setShowSettings(false)} />}
        {chatVisible && (
          <aside
            onPointerDown={startChatDrag}
            onPointerDownCapture={startChatResize}
            onPointerMove={moveChatDrag}
            onPointerMoveCapture={moveChatResize}
            onPointerUp={stopChatDrag}
            onPointerCancel={stopChatDrag}
            className={chatFloating ? 'absolute z-[130] h-[760px] max-h-[calc(100dvh-4.5rem)] w-[420px] max-w-[calc(100vw-1rem)] overflow-hidden rounded-[12px] border border-white/10 bg-[#111113] shadow-[0_24px_80px_rgba(0,0,0,0.42)]' : 'absolute right-0 top-[3.5rem] z-[130] h-[calc(100dvh-3.5rem)] w-[430px] max-w-[calc(100vw-1rem)] overflow-hidden border-l border-white/10 bg-[#111113] shadow-none'}
            style={chatFloating ? { left: chatPosition.x, top: chatPosition.y, width: chatSize.width, height: chatSize.height } : undefined}
          >
            {materialScope ? <MaterialChat onClose={closeChat} /> : <ProjectCodexChat floating={chatFloating} onToggleFloating={toggleChatFloating} onClose={closeChat} />}
            {chatResizeHandles}
          </aside>
        )}
      </div>
    );
  }

  return (
    <div className={`h-[100dvh] overflow-hidden bg-[#181818] text-sceneturn-text${usesProjectNavigation ? ' scene-project-frame' : ''}`}>
      {pathWithoutLocale === '/editor' && <Suspense><OpenRequestedCodexChat onOpen={setShowChat} /></Suspense>}
      {showApplicationHeader && <AppNavigation chatOpen={chatVisible} onOpenSettings={openSettings} onToggleChat={toggleChat} windowControlsOnly={usesProjectNavigation} />}
      <div className={showApplicationHeader ? 'relative h-[calc(100dvh-3.5rem)] overflow-hidden' : 'relative h-full overflow-hidden'}>
        <ProjectChatControl.Provider value={projectChatControl}>{children}</ProjectChatControl.Provider>
        {showSettings && showApplicationHeader && <SceneTurnAiSettingsCenter onClose={() => setShowSettings(false)} />}
        {chatVisible && !assetWorkspaceOpen && !(usesStandaloneMaterials && pathWithoutLocale === '/studio') && (
          <aside
            hidden={usesProjectNavigation && isPreviewPlaying}
            onPointerDown={startChatDrag}
            onPointerDownCapture={startChatResize}
            onPointerMove={moveChatDrag}
            onPointerMoveCapture={moveChatResize}
            onPointerUp={stopChatDrag}
            onPointerCancel={stopChatDrag}
            className={chatFloating ? 'absolute z-[130] h-[760px] max-h-[calc(100%-1rem)] w-[420px] max-w-[calc(100vw-1rem)] overflow-hidden rounded-[12px] border border-white/10 bg-[#111113] shadow-[0_24px_80px_rgba(0,0,0,0.42)]' : `absolute right-0 z-[130] max-w-[calc(100vw-1rem)] overflow-hidden border-l border-white/10 bg-[#111113] shadow-none ${usesProjectNavigation ? 'scene-project-chat-dock' : 'top-0 h-full w-[430px]'}`}
            style={chatFloating ? { left: chatPosition.x, top: chatPosition.y, width: chatSize.width, height: chatSize.height } : undefined}
          >
            {usesStandaloneMaterials ? <MaterialChat onClose={closeChat} /> : <ProjectCodexChat floating={chatFloating} onToggleFloating={toggleChatFloating} onClose={closeChat} />}
            {chatResizeHandles}
          </aside>
        )}
      </div>
    </div>
  );
}
