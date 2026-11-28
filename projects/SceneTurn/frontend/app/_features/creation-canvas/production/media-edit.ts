import type { CanvasNodeData } from '../upstream/canvas';

export type SourceClip = { assetId: string; start: number; end: number; gain: number; fadeIn: number; fadeOut: number; offset: number };
export type Caption = { start: number; end: number; text: string };
export type CropRegion = { x: number; y: number; right: number; bottom: number };
export type EditSettings = { keepSourceFormat?:boolean; crop?: CropRegion; clips?: Record<string, SourceClip>; order?: string[]; captions?: Caption[]; ratio?: '16:9' | '9:16' | '1:1'; resolution?: 480 | 720 | 1080; fps?: 24 | 25 | 30 | 60; container?: 'mp4' | 'webm' };
export type AudioMode = '配音旁白' | '音乐配乐' | '音效' | '音色生成';
export type AudioDraft = { prompt?: string; voice?: string; style?: string; language?: string; instrument?: boolean; singer?: string; lyrics?: string; speed?: number; pitch?: number; volume?: number };
export type LocalAudioMode = 'music'|'effects'|'voice';
export type SavedAudioJob = { id:string;mode:LocalAudioMode;request?:{prompt:string;lyrics:string;referenceAssetId?:string;voiceConsent:boolean} };
export type AudioSettings = { mode?: AudioMode; drafts?: Partial<Record<AudioMode, AudioDraft>>; job?:SavedAudioJob };

export function clipFor(node: CanvasNodeData, settings: EditSettings): SourceClip {
  const id = node.metadata?.storageKey || '';
  const saved = settings.clips?.[node.id];
  if (saved?.assetId === id) return saved;
  return { assetId: id, start: 0, end: (node.metadata?.durationMs || 0) / 1000, gain: 1, fadeIn: 0, fadeOut: 0, offset: 0 };
}

export async function renderMediaEdit(projectId: string, settings: EditSettings, sources: CanvasNodeData[], audioOnly: boolean) {
  const container = audioOnly ? 'wav' : settings.container || 'mp4';
  const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/media-tools/render`, {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      videos: sources.filter(n => n.type === 'video').map(n => clipFor(n, settings)),
      audio: sources.filter(n => n.type === 'audio').map(n => clipFor(n, settings)),
      ratio: settings.ratio || '16:9', resolution: settings.resolution || 720,
      fps: settings.fps || 30, container, captions: audioOnly ? [] : settings.captions || [], keepSourceFormat:!audioOnly && !!settings.keepSourceFormat,
      crop: settings.crop,
    }),
  });
  if (!response.ok) {
    const reason = response.headers.get('content-type')?.includes('json') ? await response.json() : null;
    throw new Error(reason?.error?.message || `导出失败（${response.status}）`);
  }
  const media = await response.blob();
  return new File([media], `${audioOnly ? '音频剪辑' : '素材混剪'}.${container}`, { type: media.type });
}

export function downloadMedia(file: File) {
  const url = URL.createObjectURL(file);
  const a = document.createElement('a');
  a.href = url; a.download = file.name; a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}
