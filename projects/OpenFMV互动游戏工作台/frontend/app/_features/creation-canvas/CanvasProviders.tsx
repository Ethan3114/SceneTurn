'use client';

import type { ReactNode } from 'react';
import { App, ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { I18nextProvider } from 'react-i18next';

import canvasI18n from './i18n';
import { canvasThemes } from './upstream/canvas-theme';
import { useThemeStore } from './upstream/use-theme-store';
import './canvas.css';

export function CanvasProviders({ children }: { children: ReactNode }) {
  const mode = useThemeStore((state) => state.theme);
  const colors = canvasThemes[mode];
  return <I18nextProvider i18n={canvasI18n}><ConfigProvider locale={zhCN} theme={{ algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm, token: { colorBgContainer: colors.node.panel, colorBgElevated: colors.node.panel, colorText: colors.node.text, colorBorder: colors.node.stroke, colorPrimary: '#89704b', borderRadius: 10, fontFamily: 'inherit' } }}><App className={`fmv-creation-surface ${mode}`} style={{ color: colors.node.text, background: colors.canvas.background }}>{children}</App></ConfigProvider></I18nextProvider>;
}
