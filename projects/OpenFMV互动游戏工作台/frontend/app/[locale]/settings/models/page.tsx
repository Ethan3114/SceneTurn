import { Suspense } from 'react';
import ModelsSettingsPage from '@/app/_components/workbench/ModelsSettingsPage';

export default function Page() {
  return <Suspense fallback={<main className="fmv-loading">正在打开模型设置…</main>}><ModelsSettingsPage /></Suspense>;
}
