import { SceneTurnWork } from '@/app/_components/showcase/SceneTurnPublic';

export default async function WorkPage({ params }: { params: Promise<{ locale: string; slug: string }> }) {
  const { locale, slug } = await params;
  return <SceneTurnWork key={slug} locale={locale} slug={slug} />;
}
