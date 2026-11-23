'use client';

import { useLocale, useTranslations } from 'next-intl';

import { PublicShell } from './SceneTurnPublic';
import './sceneturn-legal.css';

const sections = {
  privacy: ['account', 'generation', 'publication'],
  terms: ['content', 'generation', 'publication'],
} as const;

export default function SceneTurnLegalPage({ document }: { document: keyof typeof sections }) {
  const locale = useLocale();
  const t = useTranslations('legal');

  return (
    <PublicShell locale={locale}>
      <main id="st-main" className="st-legal">
        <article className="st-legal-paper" aria-labelledby="st-legal-title">
          <header>
            <h1 id="st-legal-title">{t(`${document}.title`)}</h1>
            <p>{t(`${document}.description`)}</p>
          </header>
          {sections[document].map((section) => (
            <section key={section} className="st-legal-section">
              <h2>{t(`${document}.${section}.title`)}</h2>
              <p>{t(`${document}.${section}.body`)}</p>
            </section>
          ))}
        </article>
      </main>
    </PublicShell>
  );
}
