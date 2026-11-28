import { createInstance } from 'i18next';
import { initReactI18next } from 'react-i18next';

import zhCN from './upstream/locales/zh-CN';
import enUS from './upstream/locales/en-US';

const canvasI18n = createInstance();
void canvasI18n.use(initReactI18next).init({
  resources: { 'zh-CN': { translation: zhCN }, 'en-US': { translation: enUS } },
  lng: 'zh-CN', fallbackLng: 'zh-CN', initAsync: false,
  interpolation: { escapeValue: false }, react: { useSuspense: false },
});
export default canvasI18n;
