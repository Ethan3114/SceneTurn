import Image from 'next/image';
import Link from 'next/link';
import { getTranslations } from 'next-intl/server';

import { ArrowRight, ChevronDown, ChevronRight, ExternalLink, Film, FolderOpen, GitBranch, Play, Sparkles } from 'lucide-react';

import SceneTurnMark from '@/app/_components/workbench/SceneTurnMark';
import { getLocalizedPath } from '@/app/_utils/localePaths';

import { HomeFaqSection, HomeFeatureSection } from './HomeDetails';
import { HomeNavigation } from './HomeNavigation';
import { HomeTrial } from './HomeTrial';
import type { HomeTrialCopy } from './HomeTrial';
import { SceneTurnHomePremiere } from './SceneTurnPublic';
import styles from './sceneturn-home.module.css';

const repository = 'https://github.com/Ethan3114/SceneTurn';
const workbenchPreviews = ['blueprint', 'timeline', 'player', 'assets'] as const;
const questions = ['materials', 'models', 'codex', 'play', 'publish'] as const;

export async function SceneTurnHome({ locale, premiereSlug }: { locale: string; premiereSlug?: string }) {
  const [t, legal] = await Promise.all([
    getTranslations({ locale, namespace: 'home' }),
    getTranslations({ locale, namespace: 'legal' }),
  ]);
  const homeHref = getLocalizedPath(locale, '/');
  const studioHref = getLocalizedPath(locale, '/projects');
  const navigation = {
    label: t('nav.label'),
    product: t('nav.product'),
    features: t('nav.features'),
    menu: t('nav.menu'),
    studio: t('nav.studio'),
    github: t('nav.github'),
    close: t('nav.close'),
  };
  const trial: HomeTrialCopy = {
    heading: t('trial.heading'),
    introduction: t('trial.introduction'),
    storyTitle: t('trial.storyTitle'),
    startPrompt: t('trial.startPrompt'),
    startLabel: t('trial.startLabel'),
    replayHint: t('trial.replayHint'),
    decisionTitle: t('trial.decisionTitle'),
    decisionPrompt: t('trial.decisionPrompt'),
    signalLabel: t('trial.signalLabel'),
    signalHint: t('trial.signalHint'),
    gateLabel: t('trial.gateLabel'),
    gateHint: t('trial.gateHint'),
    signalTitle: t('trial.signalTitle'),
    signalOutcome: t('trial.signalOutcome'),
    gateTitle: t('trial.gateTitle'),
    gateOutcome: t('trial.gateOutcome'),
    retryLabel: t('trial.retryLabel'),
    backLabel: t('trial.backLabel'),
  };
  const capabilities = [
    { id: 'material', Icon: Sparkles, imageSrc: '/sceneturn/home/ai.webp' },
    { id: 'codex', Icon: GitBranch, imageSrc: '/sceneturn/home/codex.webp' },
  ] as const;
  const workflow = [
    { id: 'material', Icon: FolderOpen },
    { id: 'story', Icon: GitBranch },
    { id: 'interaction', Icon: Film },
    { id: 'publish', Icon: Play },
  ] as const;

  return (
    <div className={styles.page} data-home>
      <a className={styles.skip} href="#st-main">{legal('skip')}</a>
      <HomeNavigation copy={navigation} locale={locale} homeHref={homeHref} studioHref={studioHref} githubHref={repository} />
      <main id="st-main" tabIndex={-1}>
        <section className={styles.hero} aria-labelledby="sceneturn-home-heading">
          <div className={styles.heroArtwork}>
            <Image src="/sceneturn/tide.jpg" alt={t('hero.artAlt')} fill sizes="(max-width: 760px) 100vw, 67vw" preload unoptimized />
          </div>
          <div className={styles.wrap}>
            <div className={styles.heroCopy}>
              <p className={styles.eyebrow}>SceneTurn</p>
              <h1 id="sceneturn-home-heading"><span>{t('hero.headlineFirst')}</span><span>{t('hero.headlineSecond')}</span></h1>
              <p className={styles.heroDescription}>{t('hero.description')}</p>
              <div className={styles.actions}>
                <Link href={studioHref} className={styles.primary}>
                  <ArrowRight size={18} aria-hidden="true" />{t('nav.studio')}
                </Link>
                <a href={repository} target="_blank" rel="noreferrer" className={styles.outline}>
                  <ExternalLink size={17} aria-hidden="true" />{t('nav.github')}
                </a>
              </div>
            </div>
            <a className={styles.explore} href="#product"><ChevronDown size={18} aria-hidden="true" />{t('hero.explore')}</a>
          </div>
        </section>

        <section id="product" className={styles.product} aria-labelledby="sceneturn-home-product">
          <div className={styles.wrap}>
            <div className={styles.sectionIntro} data-home-reveal>
              <h2 id="sceneturn-home-product">{t('product.title')}</h2>
              <p>{t('product.description')}</p>
            </div>
            <div className={styles.capabilities}>
              {capabilities.map(({ id, Icon, imageSrc }) => (
                <article className={styles.capability} key={id} data-home-reveal>
                  <h3><Icon size={22} aria-hidden="true" />{t(`product.${id}.title`)}</h3>
                  <p>{t(`product.${id}.description`)}</p>
                  <figure>
                    <Image src={imageSrc} alt={t(`product.${id}.alt`)} width={1440} height={900} sizes="(min-width: 1200px) 42vw, (min-width: 761px) 43vw, 90vw" unoptimized />
                    <figcaption>{t(`product.${id}.caption`)}</figcaption>
                  </figure>
                </article>
              ))}
            </div>
            <a href="#trial" className={styles.paperButton}><Play size={17} aria-hidden="true" />{t('product.trialLink')}</a>
          </div>
        </section>

        <HomeTrial copy={trial} />
        {premiereSlug && <SceneTurnHomePremiere locale={locale} slug={premiereSlug} />}

        <HomeFeatureSection
          title={t('features.title')}
          description={t('features.description')}
          previewLabel={t('features.previewLabel')}
          items={workbenchPreviews.map((id) => ({
            id,
            title: t(`features.${id}.title`),
            description: t(`features.${id}.description`),
            previewTitle: t(`features.${id}.previewTitle`),
            imageSrc: `/sceneturn/home/${id}.webp`,
            imageAlt: t(`features.${id}.imageAlt`),
          }))}
        />

        <section className={styles.workflow} aria-labelledby="sceneturn-home-workflow">
          <div className={styles.wrap}>
            <div className={styles.sectionIntro}>
              <h2 id="sceneturn-home-workflow">{t('workflow.title')}</h2>
              <p>{t('workflow.description')}</p>
            </div>
            <ol className={styles.steps}>
              {workflow.map(({ id, Icon }, position) => (
                <li key={id}>
                  <Icon size={23} aria-hidden="true" />
                  <h3>{t(`workflow.${id}.title`)}</h3>
                  <p>{t(`workflow.${id}.description`)}</p>
                  {position < workflow.length - 1 && <ChevronRight size={17} className={styles.stepArrow} aria-hidden="true" />}
                </li>
              ))}
            </ol>
          </div>
        </section>

        <HomeFaqSection
          title={t('faq.title')}
          description={t('faq.description')}
          settingsLabel={t('faq.settingsLabel')}
          settingsHref={getLocalizedPath(locale, '/settings/models')}
          items={questions.map((id) => ({ id, question: t(`faq.${id}.question`), answer: t(`faq.${id}.answer`) }))}
        />
      </main>

      <footer className={styles.footer}>
        <div className={styles.wrap}>
          <h2>{t('footer.title')}</h2>
          <div className={styles.actions}>
            <Link href={studioHref} className={styles.primary}>
              <ArrowRight size={18} aria-hidden="true" />{t('nav.studio')}
            </Link>
            <a href={repository} target="_blank" rel="noreferrer" className={styles.outline}>
              <ExternalLink size={17} aria-hidden="true" />{t('nav.github')}
            </a>
          </div>
          <div className={styles.footerBar}>
            <Link href={homeHref} className={styles.brand}>
              <SceneTurnMark size={24} monochrome />
              <span>SceneTurn</span>
            </Link>
            <nav aria-label={legal('navigation')}>
              <Link href={getLocalizedPath(locale, '/works')}>{t('footer.works')}</Link>
              <Link href={getLocalizedPath(locale, '/privacy')}>{legal('privacy.title')}</Link>
              <Link href={getLocalizedPath(locale, '/terms')}>{legal('terms.title')}</Link>
            </nav>
          </div>
        </div>
      </footer>
    </div>
  );
}
