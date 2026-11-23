import type { ModelProfile } from '@/app/_utils/providerApi';
import type { CanvasNodeMetadata } from './upstream/canvas';

export type CanvasVideoSourceMode = NonNullable<CanvasNodeMetadata['videoSourceMode']>;

export const videoSourceLabels = {
  text: '文生视频',
  frames: '首尾帧',
  reference: '全能参考',
  edit: '视频编辑',
  extend: '视频续写',
} satisfies Record<CanvasVideoSourceMode, string>;

type CanvasVideoOption = { id: string; label: string; mode?: CanvasVideoSourceMode };

const videoOption = (mode: CanvasVideoSourceMode): CanvasVideoOption => ({ id: mode, label: videoSourceLabels[mode], mode });
const wanVideoOptions = (['text', 'reference', 'frames', 'extend'] as const).map(videoOption);
const seedanceFlashVideoOptions: CanvasVideoOption[] = [
  videoOption('text'), videoOption('reference'),
  { id: 'image-to-video', label: '图生视频', mode: 'frames' }, videoOption('frames'),
  { id: 'image-reference', label: '图片参考', mode: 'reference' }, videoOption('edit'), videoOption('extend'),
  { id: 'long-video', label: '超长视频 Beta' },
];

export function canvasVideoModeSupported(mode: CanvasVideoSourceMode, profile?: ModelProfile) {
  if (mode === 'text') return true;
  if (mode === 'frames') return Boolean(profile?.capabilities.firstFrame);
  return Boolean(profile?.capabilities.videoInputModes?.includes(mode));
}

export function canvasVideoModeOptions(profile: ModelProfile | undefined, imageCount: number, videoCount: number) {
  const options = profile?.id === 'wan-3-0' ? wanVideoOptions : profile?.id === 'seedance-2-5-flash' ? seedanceFlashVideoOptions : [
    videoOption('text'),
    ...(profile?.capabilities.firstFrame ? [{ ...videoOption('frames'), label: profile.capabilities.lastFrame ? '首尾帧' : '首帧' }] : []),
    ...(profile?.capabilities.videoInputModes ?? []).map(videoOption),
  ];
  return options.map((option) => {
    const { id, mode } = option;
    const supported = Boolean(mode && canvasVideoModeSupported(mode, profile) && (id !== 'image-reference' || profile?.capabilities.maxReferenceImages));
    const hasInput = mode === 'text' || (mode === 'frames' || id === 'image-reference' ? imageCount > 0
      : mode === 'reference' ? videoCount > 0 || Boolean(profile?.capabilities.maxReferenceImages && imageCount) : videoCount > 0);
    return { ...option, disabled: !supported || !hasInput, reason: !supported ? '当前模型连接未开放此方式' : !hasInput ? '请先添加该方式需要的参考素材' : undefined };
  });
}

export function canvasVideoReferenceImageIds(metadata: CanvasNodeMetadata | undefined, connectedImageNodeIds: string[]) {
  const connected = new Set(connectedImageNodeIds);
  if (metadata?.videoReferenceImageNodeIds === undefined) return [...connected];
  return [...new Set(metadata.videoReferenceImageNodeIds)].filter((id) => connected.has(id));
}

export function canvasVideoRatios(metadata?: CanvasNodeMetadata, profile?: ModelProfile) {
  if (metadata?.videoSourceMode === 'edit' || metadata?.videoSourceMode === 'extend'
    || (metadata?.videoSourceMode === 'frames' && profile?.capabilities.fixedFrameRatioMode === 'adaptive')) return ['adaptive'];
  return profile?.capabilities.ratios ?? ['16:9'];
}

export function canvasVideoRatio(metadata?: CanvasNodeMetadata, profile?: ModelProfile) {
  const ratios = canvasVideoRatios(metadata, profile);
  const ratio = metadata?.videoRatio ?? (profile?.id === 'wan-3-0' ? 'adaptive' : '16:9');
  return ratios.includes(ratio) ? ratio : ratios[0];
}
