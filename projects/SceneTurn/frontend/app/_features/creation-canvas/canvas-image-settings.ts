import type { ModelProfile } from '@/app/_utils/providerApi';
import type { CanvasNodeMetadata } from './upstream/canvas';

export const imageRatios = [
  { id: '1:1', width: 1, height: 1 },
  { id: '1:2', width: 1, height: 2 },
  { id: '2:1', width: 2, height: 1 },
  { id: '9:16', width: 9, height: 16 },
  { id: '16:9', width: 16, height: 9 },
  { id: '3:4', width: 3, height: 4 },
  { id: '4:3', width: 4, height: 3 },
  { id: '3:2', width: 3, height: 2 },
  { id: '2:3', width: 2, height: 3 },
  { id: '5:4', width: 5, height: 4 },
  { id: '4:5', width: 4, height: 5 },
  { id: '21:9', width: 7, height: 3 },
  { id: '9:21', width: 3, height: 7 },
];

export function imageRatioFor(metadata?: CanvasNodeMetadata) {
  const width = metadata?.imageWidth;
  const height = metadata?.imageHeight;
  return imageRatios.find((ratio) => width && height && width * ratio.height === height * ratio.width) ?? imageRatios.find((ratio) => ratio.id === '16:9')!;
}

export function imageSizeOptions(ratio: typeof imageRatios[number], profile?: ModelProfile) {
  const minimum = profile?.capabilities.minDimension ?? 512;
  const maximum = profile?.capabilities.maxDimension ?? 4096;
  const minPixels = profile?.capabilities.minPixels ?? 0;
  const maxPixels = profile?.capabilities.maxPixels ?? Infinity;
  return [1024, 2048, 3072, 4096].flatMap((longEdge) => {
    const unit = Math.floor(longEdge / (Math.max(ratio.width, ratio.height) * 32)) * 32;
    const imageWidth = ratio.width * unit;
    const imageHeight = ratio.height * unit;
    return Math.min(imageWidth, imageHeight) >= minimum && Math.max(imageWidth, imageHeight) <= maximum && imageWidth * imageHeight >= minPixels && imageWidth * imageHeight <= maxPixels
      ? [{ imageWidth, imageHeight, longEdge }] : [];
  });
}

export function canvasImageSize(metadata?: CanvasNodeMetadata, profile?: ModelProfile) {
  const options = imageSizeOptions(imageRatioFor(metadata), profile);
  return options.find((size) => size.imageWidth === metadata?.imageWidth && size.imageHeight === metadata?.imageHeight)
    ?? options.find((size) => size.longEdge === 2048) ?? options[0];
}
