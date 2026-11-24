'use client';

import React, { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Check, ChevronLeft, Clock3, Download, Globe2, Loader2, MessageSquare, MonitorDown, Play, Settings, Upload } from 'lucide-react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useShallow } from 'zustand/react/shallow';

import { useProjectSessionStore } from '@/app/_features/project-session/store';
import { useRuntimeSessionStore } from '@/app/_features/runtime-session/store';
import { useClickOutside } from '@/app/_hooks/useClickOutside';
import { isAssetWorkspaceOpen, useEditorStore } from '@/app/_store/useEditorStore';
import { usePlayerStore } from '@/app/_store/usePlayerStore';
import { getLocalizedPath, stripLocaleFromPath } from '@/app/_utils/localePaths';
import { getEntryNodeId } from '@/app/_utils/graphRuntime';
import { Input } from '../ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '../ui/select';
import ProjectTransfers from '@/app/_features/project-transfers/ProjectTransfers';
import { useMaterialWorkspace } from '@/app/_features/material-workspace/store';
import SceneTurnMark from '@/app/_components/workbench/SceneTurnMark';
import { ProjectChatControl } from '@/app/_components/local/AppWindowFrame';
import { syncProjectSession } from '@/app/_utils/syncProjectSession';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '../ui/dialog';
import './material-shell.css';

const AUTOSAVE_DELAY_MS = 1200;

export default function TopBar() {
  const locale = useLocale();
  const t = useTranslations('editor');
  const assetsT = useTranslations('assets');
  const navigationT = useTranslations('navigation');
  const projectsT = useTranslations('projects');
  const projectChat = useContext(ProjectChatControl);
  const {
    project,
    projectId,
    title,
    status,
    dirty,
    revision,
    loadProject,
    setTitle,
    saveNow,
    flushPendingChanges,
    getGraphSnapshot,
  } = useProjectSessionStore(
    useShallow((state) => ({
      project: state.project,
      projectId: state.projectId,
      title: state.title,
      status: state.status,
      dirty: state.dirty,
      revision: state.revision,
      loadProject: state.loadProject,
      setTitle: state.setTitle,
      saveNow: state.saveNow,
      flushPendingChanges: state.flushPendingChanges,
      getGraphSnapshot: state.getGraphSnapshot,
    }))
  );
  const {
    autoSaveEnabled,
    setAutoSaveEnabled,
    edgeCurveStyle,
    setEdgeCurveStyle,
  } = useEditorStore(
    useShallow((state) => ({
      autoSaveEnabled: state.autoSaveEnabled,
      setAutoSaveEnabled: state.setAutoSaveEnabled,
      edgeCurveStyle: state.edgeCurveStyle,
      setEdgeCurveStyle: state.setEdgeCurveStyle,
    }))
  );
  const { setIsPlaying, setCurrentNode, reset } = usePlayerStore();
  const startRuntimeSession = useRuntimeSessionStore((state) => state.start);
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const routeProjectId = searchParams.get('id');
  const initialTitleFromQuery = searchParams.get('title')?.trim();
  const [isExporting, setIsExporting] = useState(false);
  const [exportStatus, setExportStatus] = useState('');
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isExportMenuOpen, setIsExportMenuOpen] = useState(false);
  const [isTransferOpen, setIsTransferOpen] = useState(false);
  const [projectActionPending, setProjectActionPending] = useState(false);
  const [projectActionError, setProjectActionError] = useState('');
  const settingsRef = useRef<HTMLDivElement>(null);
  const exportMenuRef = useRef<HTMLDivElement>(null);
  const exportStatusTimerRef = useRef<number | null>(null);
  const navigationParams = new URLSearchParams(searchParams.toString());
  const openAssetsFromRoute = navigationParams.get('workspace') === 'assets';
  if (openAssetsFromRoute) navigationParams.delete('workspace');
  if (!navigationParams.get('id') && projectId) navigationParams.set('id', projectId);
  const queryString = navigationParams.toString();
  const querySuffix = queryString ? `?${queryString}` : '';
  const isNodeMode = stripLocaleFromPath(pathname).startsWith('/nodes');
  const showAssetWorkspace = useEditorStore(isAssetWorkspaceOpen) && !isNodeMode;
  const assetWorkspaceView = useEditorStore((state) => state.assetWorkspaceView);
  const assetChatOpen = useMaterialWorkspace((state) => state.chatOpen);
  const blueprintHref = getLocalizedPath(locale, `/editor${querySuffix}`);
  const nodesHref = getLocalizedPath(locale, `/nodes${querySuffix}`);
  const activeView = showAssetWorkspace ? 'assets' : isNodeMode ? 'timeline' : 'story';
  const chatOpen = showAssetWorkspace ? assetWorkspaceView === 'generate' && assetChatOpen : projectChat?.open ?? false;
  const lastSaved = project?.updatedAt ? new Date(project.updatedAt) : null;

  useClickOutside(settingsRef as React.RefObject<HTMLElement>, () => {
    if (isSettingsOpen) setIsSettingsOpen(false);
  });

  useClickOutside(exportMenuRef as React.RefObject<HTMLElement>, () => {
    if (isExportMenuOpen) setIsExportMenuOpen(false);
  });

  const flushProjectSession = useCallback(() => {
    void flushPendingChanges().catch((error) => {
      console.error('Failed to flush local project before navigation', error);
    });
  }, [flushPendingChanges]);

  useEffect(() => {
    void loadProject(routeProjectId).catch((error) => {
      console.error('Failed to load local project', error);
    });
  }, [loadProject, routeProjectId]);

  useEffect(() => {
    if (routeProjectId || !initialTitleFromQuery) return;
    if (title !== initialTitleFromQuery) setTitle(initialTitleFromQuery);
  }, [initialTitleFromQuery, routeProjectId, setTitle, title]);

  useEffect(() => {
    if (isNodeMode || !openAssetsFromRoute) return;
    const editor = useEditorStore.getState();
    editor.setTargetNodeIdForAsset(null);
    editor.setAssetPickerOpen(true);
    router.replace(blueprintHref, { scroll: false });
  }, [blueprintHref, isNodeMode, openAssetsFromRoute, router]);

  useEffect(() => {
    return () => {
      if (exportStatusTimerRef.current) window.clearTimeout(exportStatusTimerRef.current);
    };
  }, []);

  useEffect(() => {
    return () => {
      flushProjectSession();
    };
  }, [flushProjectSession]);

  useEffect(() => {
    if (!autoSaveEnabled || !dirty || status === 'loading' || status === 'saving') return;
    const timer = window.setTimeout(() => {
      void saveNow().catch((error) => {
        console.error('Failed to auto-save local project', error);
      });
    }, AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [autoSaveEnabled, dirty, revision, saveNow, status]);

  const isProjectSaving = status === 'saving';
  const isProjectLoading = status === 'loading' || Boolean(routeProjectId && routeProjectId !== projectId);
  const saveStatus = !autoSaveEnabled
    ? { label: t('autoSavePaused'), icon: Clock3, spin: false }
    : isProjectSaving
      ? { label: t('saving'), icon: Loader2, spin: true }
      : dirty
        ? { label: t('autoSaving'), icon: Clock3, spin: false }
        : { label: t('autoSaved'), icon: Check, spin: false };
  const SaveStatusIcon = saveStatus.icon;

  const handlePlay = () => {
    const graph = getGraphSnapshot();
    const entryNodeId = getEntryNodeId(graph, project?.metadata.entryNodeId);
    const startNode = graph.nodes.find((node) => node.id === entryNodeId) ?? graph.nodes[0];
    if (!startNode) return;
    reset();
    startRuntimeSession(graph, { entryNodeId: startNode.id });
    setCurrentNode(startNode.id);
    setIsPlaying(true);
  };

  const showExportStatus = (message: string) => {
    if (exportStatusTimerRef.current) window.clearTimeout(exportStatusTimerRef.current);
    setExportStatus(message);
    exportStatusTimerRef.current = window.setTimeout(() => {
      setExportStatus('');
      exportStatusTimerRef.current = null;
    }, 3600);
  };

  const handleExport = async () => {
    setIsExportMenuOpen(false);
    if (!window.openfmv?.exportGame || !window.openfmv?.selectDirectory) {
      setIsTransferOpen(true);
      return;
    }

    showExportStatus(t('exporting'));
    setIsExporting(true);
    try {
      const savedProject = await saveNow();
      if (!savedProject) return;
      const outputDirectory = await window.openfmv.selectDirectory();
      if (!outputDirectory) return;
      await window.openfmv.exportGame(savedProject, {
        gameTitle: savedProject.title,
        outputDirectory,
        locale,
        entryNodeId: savedProject.metadata.entryNodeId,
        windowMode: 'windowed',
        resolution: { width: 1280, height: 720 },
        includeDebugOverlay: false,
      });
      showExportStatus(t('exportComplete'));
      alert(t('exportComplete'));
    } catch (error) {
      console.error('Failed to export game', error);
      alert(t('exportFailed'));
    } finally {
      setIsExporting(false);
    }
  };

  async function runProjectAction(action: () => void) {
    if (projectActionPending) return;
    setProjectActionPending(true);
    setProjectActionError('');
    try {
      if (showAssetWorkspace) {
        await useMaterialWorkspace.getState().savePending();
        if (projectId) await syncProjectSession(projectId);
      }
      await flushPendingChanges();
      action();
    } catch (error) {
      setProjectActionError(error instanceof Error ? error.message : t('saveLocalFailed'));
    } finally {
      setProjectActionPending(false);
    }
  }

  function openProjectView(view: typeof activeView) {
    if (view === activeView) return;
    void runProjectAction(() => {
      const editor = useEditorStore.getState();
      if (view === 'assets') {
        if (isNodeMode) {
          navigationParams.set('workspace', 'assets');
          router.push(getLocalizedPath(locale, `/editor?${navigationParams.toString()}`));
        } else {
          editor.setTargetNodeIdForAsset(null);
          editor.setAssetPickerOpen(true);
        }
        return;
      }
      if (showAssetWorkspace) editor.setAssetPickerOpen(false);
      if (view === 'timeline') router.push(nodesHref);
      else if (isNodeMode) router.push(blueprintHref);
    });
  }

  function navigateAway(target: string) {
    void runProjectAction(() => {
      if (showAssetWorkspace) useEditorStore.getState().setAssetPickerOpen(false);
      router.push(target);
    });
  }

  function toggleChat() {
    if (!showAssetWorkspace) {
      projectChat?.toggle();
      return;
    }
    if (assetWorkspaceView === 'generate') {
      useMaterialWorkspace.setState({ chatOpen: !assetChatOpen });
      return;
    }
    void runProjectAction(() => useEditorStore.getState().setAssetWorkspaceView('generate'));
  }

  return (
    <>
      <header className="scene-material-topbar" data-testid={showAssetWorkspace ? 'material-project-nav' : undefined}>
        <div className="scene-material-project">
          <div className="scene-material-brand"><SceneTurnMark size={26} monochrome /><span>SceneTurn</span></div>
          <button type="button" className="scene-material-project-back" aria-label={locale === 'zh-CN' ? '我的项目' : 'My projects'} onClick={() => navigateAway(getLocalizedPath(locale, '/projects'))} disabled={projectActionPending}><ChevronLeft size={17} aria-hidden="true" /></button>
          <div className="scene-material-project-name">
            <Input type="text" aria-label={projectsT('projectName')} value={title} onChange={(event) => setTitle(event.target.value)} disabled={isProjectLoading || projectActionPending} />
            <div className="relative" ref={settingsRef}>
              <button type="button" className="scene-material-save-status" onClick={() => setIsSettingsOpen((value) => !value)} aria-expanded={isSettingsOpen} aria-label={`${saveStatus.label} · ${t('settings')}`} title={lastSaved ? t('lastSaved', { time: lastSaved.toLocaleTimeString() }) : t('settings')} suppressHydrationWarning>
                <span>{saveStatus.label}</span><SaveStatusIcon size={12} className={saveStatus.spin ? 'animate-spin' : undefined} aria-hidden="true" />
              </button>

              {isSettingsOpen && (
                <div className="scene-project-menu absolute left-0 top-full z-50 mt-3 w-72 overflow-hidden rounded-[10px] border border-white/15 bg-[#191919] p-1.5 shadow-[0_24px_80px_rgba(0,0,0,0.56)]">
                  <div className="px-3 py-3">
                    <button type="button" role="switch" aria-checked={autoSaveEnabled} onClick={() => setAutoSaveEnabled(!autoSaveEnabled)} className="flex h-openfmv-editor w-full items-center justify-between gap-3 rounded-openfmv-control border border-white/15 bg-white/[0.075] px-3 text-left transition hover:border-white/25 hover:bg-white/[0.10]" title={autoSaveEnabled ? t('pauseAutoSave') : t('enableAutoSave')}>
                      <span className="flex min-w-0 items-center gap-2 text-sm font-semibold text-openfmv-text">
                        <span className="truncate">{t('autoSave')}</span>
                        <SaveStatusIcon size={13} className={saveStatus.spin ? 'shrink-0 animate-spin' : 'shrink-0'} aria-hidden="true" />
                      </span>
                      <span aria-hidden="true" className={`flex h-6 w-11 shrink-0 items-center rounded-openfmv-pill border p-0.5 transition ${autoSaveEnabled ? 'border-white/40 bg-white/25' : 'border-white/15 bg-white/[0.08]'}`}>
                        <span className={`h-5 w-5 rounded-openfmv-pill bg-white shadow-[0_3px_10px_rgba(0,0,0,0.35)] transition-transform ${autoSaveEnabled ? 'translate-x-5' : 'translate-x-0'}`} />
                      </span>
                    </button>
                  </div>

                  <div className="border-t border-white/[0.08] px-3 py-3">
                    <div className="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-openfmv-muted">{t('edgeStyle')}</div>
                    <Select value={edgeCurveStyle} onValueChange={(value) => setEdgeCurveStyle(value as 'smoothstep' | 'bezier' | 'straight')}>
                      <SelectTrigger className="nodrag h-openfmv-editor rounded-openfmv-control border-white/15 bg-white/[0.075] text-sm text-openfmv-text">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="border-white/15 bg-openfmv-node text-openfmv-text">
                        <SelectItem value="smoothstep">{t('smoothStep')}</SelectItem>
                        <SelectItem value="bezier">{t('bezier')}</SelectItem>
                        <SelectItem value="straight">{t('straight')}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        <nav className="scene-material-project-tabs" aria-label="项目视图">
          <button type="button" aria-current={activeView === 'story' ? 'page' : undefined} disabled={projectActionPending} onClick={() => openProjectView('story')}>{locale === 'zh-CN' ? '剧情编排' : t('blueprintMode')}</button>
          <button type="button" aria-current={activeView === 'timeline' ? 'page' : undefined} disabled={projectActionPending} onClick={() => openProjectView('timeline')}>{locale === 'zh-CN' ? '时间线' : t('nodeMode')}</button>
          <button type="button" aria-current={activeView === 'assets' ? 'page' : undefined} disabled={projectActionPending} onClick={() => openProjectView('assets')}>{assetsT('title')}</button>
        </nav>

        <div className="scene-material-project-actions">
          {(projectChat || showAssetWorkspace) && <button type="button" className="scene-project-chat-toggle" aria-pressed={chatOpen} aria-label={navigationT('askAi')} onClick={toggleChat} disabled={!projectId || projectActionPending}><MessageSquare size={16} aria-hidden="true" /><span>{navigationT('askAi')}</span></button>}
          <button type="button" data-testid="project-play-current" aria-label={t('preview')} aria-disabled={isProjectLoading || projectActionPending} onClick={() => { if (!isProjectLoading) void runProjectAction(handlePlay); }}><Play size={15} aria-hidden="true" /><span>{t('preview')}</span></button>

          <div className="relative" ref={exportMenuRef}>
            <button type="button" data-testid="project-export-menu" aria-label={t('export')} aria-haspopup={showAssetWorkspace ? 'dialog' : 'menu'} aria-expanded={showAssetWorkspace ? isTransferOpen : isExportMenuOpen} disabled={isExporting || projectActionPending} onClick={() => {
              if (showAssetWorkspace) void runProjectAction(() => setIsTransferOpen(true));
              else setIsExportMenuOpen((value) => !value);
            }}>
              {isExporting ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <Upload size={16} aria-hidden="true" />}
              <span>{t('export')}</span>
            </button>

              {isExportMenuOpen && (
                <div role="menu" className="scene-project-menu absolute right-0 top-full z-50 mt-3 w-64 overflow-hidden rounded-[10px] border border-white/15 bg-[#191919] p-1.5 shadow-[0_24px_80px_rgba(0,0,0,0.56)]">
                  <button type="button" role="menuitem" onClick={() => void handleExport()} className="flex h-openfmv-control w-full items-center gap-2.5 rounded-openfmv-control px-2.5 text-left text-sm font-semibold text-openfmv-text transition hover:bg-white/[0.075] hover:text-white">
                    <Globe2 size={15} />
                    <span className="min-w-0 flex-1 truncate">{t('exportWebPackage')}</span>
                  </button>
                  <button type="button" role="menuitem" data-testid="project-transfer-open" onClick={() => { setIsExportMenuOpen(false); setIsTransferOpen(true); }} className="flex h-openfmv-control w-full items-center gap-2.5 rounded-openfmv-control px-2.5 text-left text-sm font-semibold text-openfmv-text transition hover:bg-white/[0.075] hover:text-white"><Download size={15} />项目备份与固定版本</button>
                  <div className="my-1 h-px bg-white/[0.08]" />
                  <button type="button" role="menuitem" disabled className="flex h-openfmv-control w-full cursor-not-allowed items-center gap-2.5 rounded-openfmv-control px-2.5 text-left text-sm font-semibold text-openfmv-muted opacity-55">
                    <MonitorDown size={15} />
                    <span className="min-w-0 flex-1 truncate">{t('exportExecutablePackage')}</span>
                    <span className="shrink-0 rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-normal text-openfmv-muted">{t('exportDisabled')}</span>
                  </button>
                </div>
              )}
          </div>
          <button type="button" title="模型设置" aria-label="模型设置" onClick={() => navigateAway(getLocalizedPath(locale, projectId ? `/settings/models?projectId=${encodeURIComponent(projectId)}` : '/settings/models'))} disabled={projectActionPending}><Settings size={17} aria-hidden="true" /></button>
        </div>
        {projectActionError && <p className="scene-material-nav-error" role="alert">{projectActionError}</p>}
        {exportStatus && <p className="scene-material-nav-error scene-project-export-status" role="status">{exportStatus}</p>}
      </header>
    <Dialog open={isTransferOpen} onOpenChange={setIsTransferOpen}><DialogContent className="fmv-transfer-dialog max-w-[820px]"><header><DialogTitle>项目备份与游戏导出</DialogTitle><button type="button" onClick={() => setIsTransferOpen(false)}>关闭导出窗口</button></header><DialogDescription>保存完整项目，或核对并导出当前游戏版本。</DialogDescription>{projectId && <ProjectTransfers projectId={projectId} />}</DialogContent></Dialog>
    </>
  );
}
