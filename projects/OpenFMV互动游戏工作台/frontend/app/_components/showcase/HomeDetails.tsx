'use client';

import { useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import Image from 'next/image';
import Link from 'next/link';

import { Check, ChevronDown, ChevronRight, Film, FolderClosed, GitBranch, Play, Plus, Settings } from 'lucide-react';

import styles from './home-details.module.css';

type FeatureItem = {
  id: string;
  title: string;
  description: string;
  previewTitle: string;
  imageSrc: string;
  imageAlt: string;
};

type FeatureProps = {
  id?: string;
  title: string;
  description: string;
  previewLabel: string;
  items: readonly FeatureItem[];
};

type FaqItem = {
  id: string;
  question: string;
  answer: string;
};

type FaqProps = {
  id?: string;
  title: string;
  description: string;
  settingsLabel: string;
  settingsHref: string;
  items: readonly FaqItem[];
};

const featureIcons = {
  blueprint: GitBranch,
  timeline: Film,
  player: Play,
  assets: FolderClosed,
};

function featureIcon(id: string) {
  return Object.hasOwn(featureIcons, id) ? featureIcons[id as keyof typeof featureIcons] : Film;
}

function HomeFeatureSection({ id = 'features', title, description, previewLabel, items }: FeatureProps) {
  const [selectedId, setSelectedId] = useState(items[0]?.id);
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});
  const activeId = items.find((item) => item.id === selectedId)?.id ?? items[0]?.id;
  const headingId = id + '-heading';

  function handleKeys(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number;
    switch (event.key) {
      case 'ArrowDown':
        nextIndex = (index + 1) % items.length;
        break;
      case 'ArrowUp':
        nextIndex = (index - 1 + items.length) % items.length;
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = items.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    const target = items[nextIndex].id;
    setSelectedId(target);
    tabs.current[target]?.focus();
  }

  return (
    <section className={styles.section} id={id} aria-labelledby={headingId}>
      <div className={styles.inner}>
        <div className={styles.heading}>
          <h2 id={headingId}>{title}</h2>
          <p>{description}</p>
        </div>
        <div className={styles.featureLayout}>
          <div className={styles.featureTabs} role="tablist" aria-labelledby={headingId} aria-orientation="vertical">
            {items.map((item, index) => {
              const Icon = featureIcon(item.id);
              const tabId = id + '-tab-' + item.id;
              const selected = item.id === activeId;
              return (
                <button
                  key={item.id}
                  ref={(button) => { tabs.current[item.id] = button; }}
                  className={styles.featureTab}
                  id={tabId}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  aria-controls={id + '-panel-' + item.id}
                  aria-labelledby={tabId + '-title'}
                  aria-describedby={tabId + '-description'}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => setSelectedId(item.id)}
                  onKeyDown={(event) => handleKeys(event, index)}
                >
                  <Icon className={styles.featureIcon} size={21} aria-hidden="true" />
                  <span className={styles.featureText}>
                    <span className={styles.featureTitle} id={tabId + '-title'}>{item.title}</span>
                    <span className={styles.featureDescription} id={tabId + '-description'}>{item.description}</span>
                  </span>
                  {selected
                    ? <Check className={styles.featureIndicator} size={20} aria-hidden="true" />
                    : <ChevronRight className={styles.featureIndicator} size={20} aria-hidden="true" />}
                </button>
              );
            })}
          </div>
          <div className={styles.featurePreview}>
            {items.map((item) => {
              const Icon = featureIcon(item.id);
              const selected = item.id === activeId;
              return (
                <div
                  key={item.id}
                  className={styles.previewPanel}
                  id={id + '-panel-' + item.id}
                  role="tabpanel"
                  aria-labelledby={id + '-tab-' + item.id + '-title'}
                  tabIndex={selected ? 0 : -1}
                  hidden={!selected}
                >
                  <figure className={styles.previewFigure}>
                    <div className={styles.previewTitle}>
                      <Icon size={18} aria-hidden="true" />
                      {item.previewTitle}
                    </div>
                    <Image
                      className={styles.previewImage}
                      src={item.imageSrc}
                      width={1440}
                      height={900}
                      sizes="(min-width: 1440px) 752px, (min-width: 900px) 55vw, 90vw"
                      alt={item.imageAlt}
                      unoptimized
                    />
                    <figcaption className={styles.previewCaption}>{previewLabel}</figcaption>
                  </figure>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}

function HomeFaqSection({ id = 'faq', title, description, settingsLabel, settingsHref, items }: FaqProps) {
  const [openId, setOpenId] = useState<string | null>(items[0]?.id ?? null);
  const headingId = id + '-heading';

  return (
    <section className={styles.section} id={id} aria-labelledby={headingId}>
      <div className={styles.inner + ' ' + styles.faqLayout}>
        <div className={styles.heading}>
          <h2 id={headingId}>{title}</h2>
          <p>{description}</p>
          <Link className={styles.settingsLink} href={settingsHref}>
            <Settings size={17} aria-hidden="true" />
            {settingsLabel}
          </Link>
        </div>
        <div className={styles.faqItems}>
          {items.map((item) => {
            const expanded = item.id === openId;
            const questionId = id + '-question-' + item.id;
            const answerId = id + '-answer-' + item.id;
            return (
              <div className={styles.faqItem} data-expanded={expanded} key={item.id}>
                <h3 className={styles.faqQuestion}>
                  <button
                    className={styles.faqButton}
                    id={questionId}
                    type="button"
                    aria-expanded={expanded}
                    aria-controls={answerId}
                    onClick={() => setOpenId((currentId) => currentId === item.id ? null : item.id)}
                  >
                    {item.question}
                    {expanded ? <ChevronDown size={21} aria-hidden="true" /> : <Plus size={21} aria-hidden="true" />}
                  </button>
                </h3>
                <div
                  className={styles.faqAnswer}
                  id={answerId}
                  role="region"
                  aria-labelledby={questionId}
                  aria-hidden={!expanded}
                >
                  <div className={styles.faqAnswerClip}>
                    <p>{item.answer}</p>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

export { HomeFaqSection, HomeFeatureSection };
