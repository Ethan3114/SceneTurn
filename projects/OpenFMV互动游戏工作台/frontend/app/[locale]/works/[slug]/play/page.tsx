import { SceneTurnPlay } from '@/app/_components/showcase/SceneTurnPublic';

export default async function PublishedPlayPage({ params }: { params: Promise<{ locale: string; slug: string }> }) {
  const { locale, slug } = await params;
  return <SceneTurnPlay key={slug} locale={locale} slug={slug} />;
}
