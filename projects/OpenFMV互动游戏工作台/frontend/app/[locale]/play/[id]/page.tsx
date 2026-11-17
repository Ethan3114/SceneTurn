import GameClient from '@/app/play/[id]/GameClient';

export default async function PlayPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ version?: string }> }) {
  const [{ id }, { version }] = await Promise.all([params, searchParams]);

  return <GameClient key={`${id}:${version ?? 'current'}`} projectId={id} versionId={version} />;
}

