import { defineRouting } from 'next-intl/routing';

export const routing = defineRouting({
  locales: ['zh-CN', 'en', 'ja', 'ko'],
  defaultLocale: 'zh-CN',
});

export type AppLocale = (typeof routing.locales)[number];
