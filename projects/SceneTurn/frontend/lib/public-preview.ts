import { hasLocale } from 'next-intl';

import { routing } from '@/i18n/routing';
import type { AppLocale } from '@/i18n/routing';

const previewMessages: Record<AppLocale, string> = {
  'zh-CN': '公开预览提供产品介绍和互动试玩。创作台暂未开放。',
  en: 'The public preview includes the product tour and interactive demo. The studio is currently closed.',
  ja: '公開プレビューでは、製品紹介とインタラクティブデモをお試しいただけます。スタジオは準備中です。',
  ko: '공개 미리 보기에서는 제품 소개와 인터랙티브 데모를 이용할 수 있습니다. 스튜디오는 아직 열리지 않았습니다.',
};

export function publicPreviewEnabled() {
  return process.env.SCENETURN_PUBLIC_PREVIEW === '1';
}

export function publicPreviewMessage(locale: string) {
  return previewMessages[hasLocale(routing.locales, locale) ? locale : routing.defaultLocale];
}
