import StudioPage from '@/app/_components/workbench/StudioPage';

export default async function Studio({ searchParams }: { searchParams: Promise<{ id?: string; sceneId?: string }> }) {
  const { id, sceneId } = await searchParams;
  return <StudioPage projectId={id || ''} initialSceneId={sceneId} />;
}
