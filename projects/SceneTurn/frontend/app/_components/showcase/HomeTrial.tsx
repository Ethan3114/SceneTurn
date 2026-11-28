'use client';

import { useEffect, useRef, useState } from 'react';
import Image from 'next/image';

import { ArrowLeft, ArrowRight, Play, RotateCcw } from 'lucide-react';

import styles from './home-trial.module.css';

type TrialPhase = 'intro' | 'choice' | 'signal' | 'gate';

export type HomeTrialCopy = {
  heading: string;
  introduction: string;
  storyTitle: string;
  startPrompt: string;
  startLabel: string;
  replayHint: string;
  decisionTitle: string;
  decisionPrompt: string;
  signalLabel: string;
  signalHint: string;
  gateLabel: string;
  gateHint: string;
  signalTitle: string;
  signalOutcome: string;
  gateTitle: string;
  gateOutcome: string;
  retryLabel: string;
  backLabel: string;
};

const artwork: Record<TrialPhase, string> = {
  intro: '/sceneturn/tide.jpg',
  choice: '/sceneturn/tide.jpg',
  signal: '/sceneturn/signal.jpg',
  gate: '/sceneturn/port.jpg',
};

export function HomeTrial({ copy }: { copy: HomeTrialCopy }) {
  const [phase, setPhase] = useState<TrialPhase>('intro');
  const sceneTitle = useRef<HTMLHeadingElement>(null);
  const hasInteracted = useRef(false);
  const sceneCopy: Record<TrialPhase, { title: string; description: string }> = {
    intro: { title: copy.storyTitle, description: copy.startPrompt },
    choice: { title: copy.decisionTitle, description: copy.decisionPrompt },
    signal: { title: copy.signalTitle, description: copy.signalOutcome },
    gate: { title: copy.gateTitle, description: copy.gateOutcome },
  };
  const scene = sceneCopy[phase];

  useEffect(() => {
    if (hasInteracted.current) sceneTitle.current?.focus({ preventScroll: true });
  }, [phase]);

  function openScene(nextPhase: TrialPhase) {
    hasInteracted.current = true;
    setPhase(nextPhase);
  }

  return (
    <section id="trial" aria-labelledby="home-trial-heading" className={styles.section}>
      <div className={styles.inner}>
        <h2 id="home-trial-heading" className={styles.heading}>{copy.heading}</h2>
        <p className={styles.introduction}>{copy.introduction}</p>
        <div className={styles.stage} data-phase={phase} key={phase}>
          <Image
            className={styles.artwork}
            src={artwork[phase]}
            alt=""
            fill
            sizes="(max-width: 1440px) 89vw, 1280px"
            loading={phase === 'intro' ? 'lazy' : 'eager'}
            unoptimized
          />
          <div className={styles.shade} aria-hidden="true" />
          {phase !== 'intro' && (
            <button type="button" className={styles.back} onClick={() => openScene('intro')}>
              <ArrowLeft size={17} aria-hidden="true" />
              {copy.backLabel}
            </button>
          )}
          <div className={styles.scene}>
            {phase !== 'intro' && <p className={styles.storyTitle}>{copy.storyTitle}</p>}
            <h3 ref={sceneTitle} tabIndex={-1} className={styles.sceneTitle}>{scene.title}</h3>
            <p className={styles.sceneDescription}>{scene.description}</p>
            {phase === 'intro' && (
              <>
                <button type="button" className={styles.primary} onClick={() => openScene('choice')}>
                  <Play size={17} aria-hidden="true" />
                  {copy.startLabel}
                </button>
                <p className={styles.hint}>{copy.replayHint}</p>
              </>
            )}
            {phase === 'choice' && (
              <div className={styles.choices}>
                <div>
                  <button type="button" className={styles.primary} aria-describedby="trial-signal-hint" onClick={() => openScene('signal')}>
                    {copy.signalLabel}
                    <ArrowRight size={17} aria-hidden="true" />
                  </button>
                  <p id="trial-signal-hint" className={styles.hint}>{copy.signalHint}</p>
                </div>
                <div>
                  <button type="button" className={styles.secondary} aria-describedby="trial-gate-hint" onClick={() => openScene('gate')}>
                    {copy.gateLabel}
                    <ArrowRight size={17} aria-hidden="true" />
                  </button>
                  <p id="trial-gate-hint" className={styles.hint}>{copy.gateHint}</p>
                </div>
              </div>
            )}
            {(phase === 'signal' || phase === 'gate') && (
              <button type="button" className={styles.primary} onClick={() => openScene('choice')}>
                <RotateCcw size={17} aria-hidden="true" />
                {copy.retryLabel}
              </button>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
