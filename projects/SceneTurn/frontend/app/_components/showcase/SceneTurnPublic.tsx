'use client';

import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { useTranslations } from 'next-intl';

import { ArrowLeft, ArrowRight, ChevronRight, Film, Play, Search } from 'lucide-react';

import { ApiError } from '@/app/_utils/cloudApi';
import { getLocalizedPath } from '@/app/_utils/localePaths';
import { getPublicWork, listPublicWorks } from '@/app/_utils/publicationApi';
import type { PlayableWork, PublicWork } from '@/app/_utils/publicationApi';
import SceneTurnMark from '@/app/_components/workbench/SceneTurnMark';

import homeStyles from './sceneturn-home.module.css';
import './sceneturn-public.css';

type Resource<T> =
  | { state: 'loading' }
  | { state: 'ready'; data: T }
  | { state: 'missing' }
  | { state: 'error' };

function usePublicationResource<T>(load: (signal: AbortSignal) => Promise<T>) {
  const [resource, setResource] = useState<Resource<T>>({ state: 'loading' });
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal).then((data) => {
      if (!controller.signal.aborted) setResource({ state: 'ready', data });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        setResource({ state: error instanceof ApiError && error.status === 404 ? 'missing' : 'error' });
      }
    });
    return () => controller.abort();
  }, [load, retryCount]);

  function retry() {
    setResource({ state: 'loading' });
    setRetryCount((value) => value + 1);
  }

  return { resource, retry };
}

function workPath(locale: string, slug: string) {
  return getLocalizedPath(locale, '/works/' + encodeURIComponent(slug));
}

function PublicHeader({ locale, current }: { locale: string; current?: 'home' | 'works' }) {
  const home = getLocalizedPath(locale, '/');
  const works = getLocalizedPath(locale, '/works');
  const t = useTranslations('legal');
  return (
    <header className="st-public-header">
      <div className="st-wrap st-public-nav">
        <Link href={home} className="st-wordmark">
          <SceneTurnMark size={26} monochrome />
          <span>SceneTurn</span>
        </Link>
        {current ? (
          <>
            <nav className="st-public-links" aria-label="作品导航">
              <Link href={home} aria-current={current === 'home' ? 'page' : undefined}>发现</Link>
              <Link href={works} aria-current={current === 'works' ? 'page' : undefined}>作品馆</Link>
            </nav>
            <div className="st-public-shortcuts">
              <Link href={getLocalizedPath(locale, '/account')} className="st-button st-button-quiet">登录</Link>
              <Link href={getLocalizedPath(locale, '/projects')} className="st-button st-button-outline">
                进入创作台 <ArrowRight size={17} aria-hidden="true" />
              </Link>
            </div>
          </>
        ) : (
          <Link href={home} className="st-button st-button-outline">
            <ArrowLeft size={17} aria-hidden="true" /> {t('backHome')}
          </Link>
        )}
      </div>
    </header>
  );
}

function PublicFooter({ locale }: { locale: string }) {
  const t = useTranslations('legal');
  return (
    <footer className="st-wrap st-public-footer">
      <Link href={getLocalizedPath(locale, '/')} className="st-wordmark">
        <SceneTurnMark size={22} monochrome />
        <span>SceneTurn</span>
      </Link>
      <nav aria-label={t('navigation')}>
        <Link href={getLocalizedPath(locale, '/privacy')}>{t('privacy.title')}</Link>
        <Link href={getLocalizedPath(locale, '/terms')}>{t('terms.title')}</Link>
      </nav>
    </footer>
  );
}

export function PublicShell({ children, locale, current }: {
  children: ReactNode;
  locale: string;
  current?: 'home' | 'works';
}) {
  const t = useTranslations('legal');
  return (
    <div className="st-public">
      <a href="#st-main" className="st-skip">{t('skip')}</a>
      <PublicHeader locale={locale} current={current} />
      {children}
      <PublicFooter locale={locale} />
    </div>
  );
}

function Artwork({ src, eager = false, sizes = '(min-width: 1000px) 30vw, 100vw' }: {
  src?: string | null;
  eager?: boolean;
  sizes?: string;
}) {
  return src
    ? <Image className="st-artwork" src={src} alt="" fill sizes={sizes} loading={eager ? 'eager' : 'lazy'} unoptimized />
    : <div className="st-artwork-placeholder" aria-hidden="true"><Film size={52} strokeWidth={1.3} /></div>;
}

function WorkCard({ locale, work }: { locale: string; work: PublicWork }) {
  return (
    <Link href={workPath(locale, work.slug)} className="st-film-card">
      <div className="st-card-art"><Artwork src={work.posterUrl} /></div>
      <div className="st-card-body">
        <h3>{work.title}</h3>
        {work.description && <p>{work.description}</p>}
        <ChevronRight size={18} className="st-card-arrow" aria-hidden="true" />
      </div>
    </Link>
  );
}

function ResourceMessage({ title, children, alert = false }: { title: string; children?: ReactNode; alert?: boolean }) {
  return (
    <div className="st-resource" role={alert ? 'alert' : 'status'}>
      <Film size={25} strokeWidth={1.5} aria-hidden="true" />
      <h2>{title}</h2>
      {children}
    </div>
  );
}

export function SceneTurnHomePremiere({ locale, slug }: { locale: string; slug: string }) {
  const t = useTranslations('home.premiere');
  const load = useCallback((signal: AbortSignal) => getPublicWork(slug, signal), [slug]);
  const { resource } = usePublicationResource(load);
  if (resource.state !== 'ready' || !resource.data.trailerUrl) return null;

  const premiere = resource.data;
  const detail = workPath(locale, premiere.slug);
  return (
    <section className={homeStyles.premiere} aria-labelledby="sceneturn-home-premiere">
      <div className={homeStyles.wrap}>
        <div className={homeStyles.premiereCard}>
          <div className={homeStyles.premiereArt}><Artwork src={premiere.posterUrl} sizes="(min-width: 850px) 36vw, 100vw" /></div>
          <div className={homeStyles.premiereCopy}>
            <p className={homeStyles.premiereLabel}>{t('label')}</p>
            <h2 id="sceneturn-home-premiere">{premiere.title}</h2>
            {premiere.description && <p>{premiere.description}</p>}
            <div className={homeStyles.actions}>
              <Link href={detail + '/trailer'} className={homeStyles.primary}><Play size={17} aria-hidden="true" />{t('trailer')}</Link>
              <Link href={detail} className={homeStyles.outline}>{t('detail')}</Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export function SceneTurnWorks({ locale }: { locale: string }) {
  const { resource, retry } = usePublicationResource(listPublicWorks);
  const [search, setSearch] = useState('');
  const term = search.trim().toLocaleLowerCase(locale);
  const results = resource.state === 'ready'
    ? resource.data.filter((work) => (work.title + ' ' + work.description).toLocaleLowerCase(locale).includes(term))
    : [];
  const featured = term ? undefined : results[0];
  const gridWorks = featured ? results.slice(1) : results;
  return (
    <PublicShell locale={locale} current="works">
      <main id="st-main" className="st-wrap st-collection">
        <h1>作品馆</h1>
        <p className="st-page-lead">探索已经可以游玩的互动电影和分支故事。</p>
        <div className="st-gallery-toolbar">
          <h2>全部作品</h2>
          <label className="st-search">
            <Search size={18} aria-hidden="true" />
            <span className="sr-only">搜索作品</span>
            <input type="search" placeholder="搜索作品" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
        </div>
        {resource.state === 'loading' && <ResourceMessage title="正在加载作品…" />}
        {(resource.state === 'error' || resource.state === 'missing') && (
          <ResourceMessage title="暂时无法读取作品馆" alert>
            <button className="st-button st-button-outline" type="button" onClick={retry}>重新加载</button>
          </ResourceMessage>
        )}
        {resource.state === 'ready' && results.length === 0 && (
          <ResourceMessage title={resource.data.length ? '没有匹配的作品' : '暂无公开作品'}>
            {resource.data.length > 0 && <button className="st-button st-button-outline" type="button" onClick={() => setSearch('')}>清除搜索</button>}
          </ResourceMessage>
        )}
        {resource.state === 'ready' && results.length > 0 && (
          <>
            {featured && <FeaturedWork locale={locale} work={featured} />}
            {gridWorks.length > 0 && (
              <div className="st-film-grid">
                {gridWorks.map((work) => <WorkCard key={work.slug} locale={locale} work={work} />)}
              </div>
            )}
          </>
        )}
      </main>
    </PublicShell>
  );
}

function FeaturedWork({ locale, work }: { locale: string; work: PublicWork }) {
  return (
    <section className="st-featured">
      <Artwork src={work.posterUrl} eager sizes="100vw" />
      <div className="st-hero-haze" />
      <div className="st-featured-copy">
        <p className="st-kicker">最新公开作品</p>
        <h2>{work.title}</h2>
        {work.description && <p className="st-summary">{work.description}</p>}
        <Link href={workPath(locale, work.slug)} className="st-button st-button-primary">
          打开作品 <ArrowRight size={17} aria-hidden="true" />
        </Link>
      </div>
    </section>
  );
}

function WorkUnavailable({ locale, resource, retry }: {
  locale: string;
  resource: Exclude<Resource<PlayableWork>, { state: 'ready' }>;
  retry: () => void;
}) {
  const title = resource.state === 'loading' ? '正在加载作品…' : '作品暂不可访问';
  return (
    <PublicShell locale={locale} current="works">
      <main id="st-main" className="st-wrap st-work-unavailable">
        <h1 className="sr-only">作品</h1>
        <ResourceMessage title={title} alert={resource.state === 'error'}>
          <div className="st-actions">
            <Link className="st-button st-button-outline" href={getLocalizedPath(locale, '/works')}>
              <ArrowLeft size={17} aria-hidden="true" /> 返回作品馆
            </Link>
            {resource.state === 'error' && <button className="st-button st-button-quiet" type="button" onClick={retry}>重新加载</button>}
          </div>
        </ResourceMessage>
      </main>
    </PublicShell>
  );
}

export function SceneTurnWork({ locale, slug }: { locale: string; slug: string }) {
  const load = useCallback((signal: AbortSignal) => getPublicWork(slug, signal), [slug]);
  const { resource, retry } = usePublicationResource(load);
  if (resource.state !== 'ready') return <WorkUnavailable locale={locale} resource={resource} retry={retry} />;
  const work = resource.data;
  return (
    <PublicShell locale={locale} current="works">
      <main id="st-main">
        <section className="st-work-cover" aria-labelledby="st-work-title">
          <Artwork src={work.posterUrl} eager sizes="100vw" />
          <div className="st-hero-haze" />
          <div className="st-wrap st-work-copy">
            <Link href={getLocalizedPath(locale, '/works')} className="st-back">
              <ArrowLeft size={17} aria-hidden="true" /> 作品馆
            </Link>
            <div className="st-work-intro">
              <p className="st-kicker">互动电影</p>
              <h1 id="st-work-title">{work.title}</h1>
              {work.description && <p className="st-work-description">{work.description}</p>}
              <div className="st-actions">
                <Link href={workPath(locale, slug) + '/play'} className="st-button st-button-primary">
                  <Play size={17} aria-hidden="true" /> 开始游戏
                </Link>
                {work.trailerUrl && (
                  <Link className="st-button st-button-outline" href={workPath(locale, slug) + '/trailer'}>观看先导片</Link>
                )}
              </div>
            </div>
          </div>
        </section>
      </main>
    </PublicShell>
  );
}

export function SceneTurnPlay({ locale, slug }: { locale: string; slug: string }) {
  const load = useCallback((signal: AbortSignal) => getPublicWork(slug, signal), [slug]);
  const { resource, retry } = usePublicationResource(load);
  if (resource.state !== 'ready') return <WorkUnavailable locale={locale} resource={resource} retry={retry} />;
  const work = resource.data;
  return (
    <main className="st-public st-cinema">
      <header className="st-cinema-header">
        <div className="st-wrap st-cinema-bar">
          <Link href={workPath(locale, slug)} className="st-back"><ArrowLeft size={17} aria-hidden="true" /> 返回作品</Link>
          <h1>{work.title}</h1>
        </div>
      </header>
      <div className="st-wrap st-cinema-body">
        <iframe title={work.title + '互动电影播放器'} src={work.playUrl} sandbox="allow-scripts" allowFullScreen referrerPolicy="no-referrer" />
      </div>
    </main>
  );
}

export function SceneTurnTrailer({ locale, slug }: { locale: string; slug: string }) {
  const load = useCallback((signal: AbortSignal) => getPublicWork(slug, signal), [slug]);
  const { resource, retry } = usePublicationResource(load);
  if (resource.state !== 'ready') return <WorkUnavailable locale={locale} resource={resource} retry={retry} />;
  const { title, posterUrl, trailerUrl } = resource.data;
  return (
    <main className="st-public st-cinema">
      <header className="st-cinema-header">
        <div className="st-wrap st-cinema-bar">
          <Link href={workPath(locale, slug)} className="st-back"><ArrowLeft size={17} aria-hidden="true" /> 返回作品</Link>
          <h1>{title} · 先导片</h1>
        </div>
      </header>
      <div className="st-wrap st-cinema-body st-trailer">
        {trailerUrl ? (
          <video src={trailerUrl} poster={posterUrl ?? undefined} controls playsInline preload="metadata" aria-label={title + '先导片'} />
        ) : (
          <ResourceMessage title="先导片尚未公开" />
        )}
      </div>
    </main>
  );
}
