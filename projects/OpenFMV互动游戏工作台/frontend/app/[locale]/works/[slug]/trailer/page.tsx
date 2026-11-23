import { SceneTurnTrailer } from '@/app/_components/showcase/SceneTurnPublic';

export default async function WorkTrailerPage({ params }: { params: Promise<{ locale: string; slug: string }> }) {
  const { locale, slug } = await params;
  return <SceneTurnTrailer locale={locale} slug={slug} />;
}
