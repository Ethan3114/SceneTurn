import { connection } from 'next/server';

import { SceneTurnHome } from '@/app/_components/showcase/SceneTurnHome';
import { publicPreviewEnabled } from '@/lib/public-preview';

export default async function LocaleHomePage({ params }: { params: Promise<{ locale: string }> }) {
  await connection();
  const { locale } = await params;
  const premiereSlug = process.env.SCENETURN_PREMIERE_PUBLICATION_SLUG;
  return <SceneTurnHome locale={locale} premiereSlug={premiereSlug} publicPreview={publicPreviewEnabled()} />;
}
