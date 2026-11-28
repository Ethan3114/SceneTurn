import WorkspaceProjectsPage from '@/app/_components/workbench/ProjectsPage';

export default async function ProjectsPage({ searchParams }: { searchParams: Promise<{ view?: string }> }) {
  return <WorkspaceProjectsPage view={(await searchParams).view === 'import' ? 'import' : 'list'} />;
}

