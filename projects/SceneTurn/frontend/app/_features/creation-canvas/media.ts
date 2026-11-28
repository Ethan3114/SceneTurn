import { PROJECT_ASSETS_SAVED_EVENT, assetContentUrl } from '@/app/_utils/cloudApi';
import { readFileAsDataUrl } from './upstream/image-utils';

let revision = 0;
export const previewUrlFor = (assetId?: string) => assetId ? assetContentUrl(assetId) : undefined;
export const getImagePreviewRevision = () => revision;
export function subscribeImagePreviews(listener: () => void) {
  const changed = () => { revision += 1; listener(); };
  window.addEventListener(PROJECT_ASSETS_SAVED_EVENT, changed);
  return () => window.removeEventListener(PROJECT_ASSETS_SAVED_EVENT, changed);
}
export async function imageToDataUrl(image: { storageKey?: string; url?: string; dataUrl?: string }) {
  const source = image.storageKey ? assetContentUrl(image.storageKey) : image.dataUrl || image.url;
  if (!source) throw new Error('参考图片没有可读取的素材地址。');
  if (source.startsWith('data:')) return source;
  const response = await fetch(source, { credentials: 'same-origin' });
  if (!response.ok) throw new Error('参考图片读取失败。');
  return readFileAsDataUrl(await response.blob());
}
