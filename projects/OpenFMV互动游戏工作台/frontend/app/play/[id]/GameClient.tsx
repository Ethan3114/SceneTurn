'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useLocale, useTranslations } from 'next-intl';
import { useRuntimeSessionStore } from '@/app/_features/runtime-session/store';
import { usePlayerStore } from '@/app/_store/usePlayerStore';
import { getLocalProject } from '@/app/_utils/localProjects';
import { getEntryNodeId } from '@/app/_utils/graphRuntime';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import PlayerOverlay from '@/app/_components/player/PlayerOverlay';
import { getCloudProject } from '@/app/_utils/cloudApi';
import { getTransfer, type ProjectTransfer } from '@/app/_utils/transferApi';

function FixedVersionPlayer({ projectId, versionId }: { projectId: string; versionId: string }) {
  const locale = useLocale();
  const [version, setVersion] = useState<ProjectTransfer | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    void getTransfer(versionId).then((value) => {
      if (value.projectId !== projectId || value.kind !== 'game' || value.status !== 'ready' || !value.previewUrl) {
        throw new Error('这个可玩版本尚未准备完成，请返回编辑页面查看。');
      }
      if (active) setVersion(value);
    }).catch((problem: unknown) => {
      if (active) setError(problem instanceof Error ? problem.message : '无法读取可玩版本。');
    });
    return () => { active = false; };
  }, [projectId, versionId]);
  return <main className="flex h-full min-h-0 flex-col bg-black text-white" data-testid="web-game-page">
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-white/15 bg-[#181715] px-4 py-3">
      <div className="min-w-0"><h1 className="truncate text-base font-semibold">{version?.projects[0]?.title || '网页游玩'}</h1><p className="text-xs text-white/60">{version ? `固定版本 v${version.sourceRevision}` : '正在读取可玩版本…'}</p></div>
      <nav className="flex items-center gap-4 text-sm" aria-label="游玩导航">
        {version && <button data-testid="web-game-restart" type="button" onClick={() => setAttempt((value) => value + 1)}>重新开始</button>}
        <Link href={getLocalizedPath(locale, `/editor?id=${encodeURIComponent(projectId)}`)}>返回编辑</Link>
        <Link href={getLocalizedPath(locale, '/projects')}>我的项目</Link>
      </nav>
    </header>
    {error ? <div className="m-auto max-w-lg p-6"><p role="alert" className="mb-4 text-red-200">{error}</p><Link href={getLocalizedPath(locale, '/account')} className="underline">前往登录</Link></div> : version?.previewUrl ?
      <iframe key={attempt} data-testid="web-game-frame" className="min-h-0 w-full flex-1 border-0" src={version.previewUrl} title="互动游戏" allow="autoplay; fullscreen" /> : <p role="status" className="m-auto p-6">正在加载游戏…</p>}
  </main>;
}

export default function GameClient({ projectId, versionId }: { projectId: string; versionId?: string }) {
  return versionId ? <FixedVersionPlayer projectId={projectId} versionId={versionId} /> : <CurrentProjectPlayer projectId={projectId} />;
}

function CurrentProjectPlayer({ projectId }: { projectId: string }) {
  const router = useRouter();
  const locale = useLocale();
  const t = useTranslations('player');
  const startRuntimeSession = useRuntimeSessionStore((state) => state.start);
  const stopRuntimeSession = useRuntimeSessionStore((state) => state.stop);
  const { isPlaying, setIsPlaying, setCurrentNode, reset: resetPlayer } = usePlayerStore();
  const [initialized, setInitialized] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    const loading = window.openfmv ? Promise.resolve(getLocalProject(projectId)) : getCloudProject(projectId).then(({ project }) => project);
    void loading.then((project) => {
      if (!active) return;
      const graphData = project?.graphData;
      if (!graphData) { setInitialized(true); return; }
      const entryNodeId = getEntryNodeId(graphData, project.metadata.entryNodeId);
      const startNode = graphData.nodes.find((node) => node.id === entryNodeId) ?? graphData.nodes[0];
      resetPlayer();
      if (startNode) {
        startRuntimeSession(graphData, { entryNodeId });
        setCurrentNode(startNode.id);
        setIsPlaying(true);
      }
      setInitialized(true);
    }).catch((problem: Error) => { if (active) { setError(problem.message); setInitialized(true); } });

    return () => {
      active = false;
      setIsPlaying(false);
      resetPlayer();
      stopRuntimeSession();
    };
  }, [projectId, resetPlayer, setCurrentNode, setIsPlaying, startRuntimeSession, stopRuntimeSession]);

  useEffect(() => {
    if (initialized && !isPlaying && !error) {
      router.push(getLocalizedPath(locale, '/projects'));
    }
  }, [initialized, isPlaying, error, locale, router]);

  return (
    <div className="flex h-full w-full items-center justify-center overflow-hidden bg-black">
      {!initialized && <div className="animate-pulse text-white">{t('loadingGame')}</div>}
      {error && <p role="alert" className="p-6 text-red-200">{error}</p>}
      {initialized && !isPlaying && <div className="text-sm text-white/60">{t('projectUnavailable')}</div>}
      <PlayerOverlay />
    </div>
  );
}
