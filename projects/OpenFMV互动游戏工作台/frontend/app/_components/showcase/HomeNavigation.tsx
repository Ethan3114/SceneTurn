'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';

import { ArrowRight, ExternalLink, Menu, X } from 'lucide-react';

import SceneTurnMark from '@/app/_components/workbench/SceneTurnMark';

import styles from './sceneturn-home.module.css';

type HomeNavigationCopy = {
  label: string;
  product: string;
  features: string;
  menu: string;
  studio: string;
  github: string;
  close: string;
};

export function HomeNavigation({ copy, locale, homeHref, studioHref, githubHref }: {
  copy: HomeNavigationCopy;
  locale: string;
  homeHref: string;
  studioHref: string;
  githubHref: string;
}) {
  const [open, setOpen] = useState(false);
  const header = useRef<HTMLElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const progress = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const homepage = header.current?.closest<HTMLElement>('[data-home]');
    if (!homepage) return;

    const number = new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 });
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame: number | null = null;

    function update() {
      frame = null;
      const offset = Math.max(0, window.scrollY);
      const distance = document.documentElement.scrollHeight - window.innerHeight;
      const percentage = distance > 0 ? Math.min(offset / distance, 1) : 0;
      if (progress.current) progress.current.textContent = number.format(percentage);
      homepage?.style.setProperty('--home-hero-progress', String(Math.min(offset / 420, 1)));
    }

    function schedule() {
      if (frame === null) frame = window.requestAnimationFrame(update);
    }

    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.intersectionRatio >= 0.35) {
          entry.target.setAttribute('data-home-revealed', '');
          observer.unobserve(entry.target);
        }
      }
    }, { threshold: 0.35 });
    const resize = new ResizeObserver(schedule);
    const revealItems = homepage.querySelectorAll('[data-home-reveal]');

    function updateMotion() {
      if (motion.matches) {
        homepage?.removeAttribute('data-home-motion');
        observer.disconnect();
      } else {
        homepage?.setAttribute('data-home-motion', '');
        revealItems.forEach((element) => {
          if (!element.hasAttribute('data-home-revealed')) observer.observe(element);
        });
      }
    }

    update();
    updateMotion();
    resize.observe(homepage);
    motion.addEventListener('change', updateMotion);
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    return () => {
      if (frame !== null) window.cancelAnimationFrame(frame);
      observer.disconnect();
      resize.disconnect();
      motion.removeEventListener('change', updateMotion);
      window.removeEventListener('scroll', schedule);
      window.removeEventListener('resize', schedule);
      homepage.removeAttribute('data-home-motion');
      homepage.style.removeProperty('--home-hero-progress');
    };
  }, [locale]);

  useEffect(() => {
    if (!open) return;
    function dismiss() {
      setOpen(false);
      trigger.current?.focus({ preventScroll: true });
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault();
        dismiss();
      }
    }
    function onPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !menu.current?.contains(event.target)) dismiss();
    }
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [open]);

  return (
    <header ref={header} className={styles.header}>
      <div className={styles.navbar}>
        <Link href={homeHref} className={styles.brand}>
          <SceneTurnMark monochrome />
          <span>SceneTurn</span>
        </Link>
        <nav className={styles.sections} aria-label={copy.label}>
          <a href="#product">{copy.product}</a>
          <a href="#features">{copy.features}</a>
        </nav>
        <div className={styles.menuGroup} ref={menu}>
          <button className={styles.menuButton} aria-label={copy.menu} aria-expanded={open} aria-controls="sceneturn-home-menu" type="button" ref={trigger} onClick={() => setOpen(!open)}>
            <Menu size={18} aria-hidden="true" />
            <span>{copy.menu}</span>
            <span aria-hidden="true" className={styles.readingProgress} ref={progress}>0%</span>
          </button>
          <div className={styles.menuPanel} id="sceneturn-home-menu" hidden={!open}>
            <div className={styles.menuTitle}>
              <strong>SceneTurn</strong>
              <button className={styles.close} type="button" aria-label={copy.close} onClick={() => {
                setOpen(false);
                trigger.current?.focus({ preventScroll: true });
              }}><X size={19} aria-hidden="true" /></button>
            </div>
            <Link className={styles.menuLink} href={studioHref} onClick={() => setOpen(false)}>
              <ArrowRight size={17} aria-hidden="true" />{copy.studio}
            </Link>
            <a className={styles.menuLink} href={githubHref} target="_blank" rel="noreferrer" onClick={() => {
              setOpen(false);
              trigger.current?.focus({ preventScroll: true });
            }}>
              <ExternalLink size={17} aria-hidden="true" />{copy.github}
            </a>
          </div>
        </div>
        <Link href={studioHref} className={styles.headerStudio}>
          <ArrowRight size={17} aria-hidden="true" />{copy.studio}
        </Link>
      </div>
    </header>
  );
}
