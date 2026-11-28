import { SceneTurnWorks } from '@/app/_components/showcase/SceneTurnPublic';

export default async function WorksPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  return <SceneTurnWorks locale={locale} />;
}
