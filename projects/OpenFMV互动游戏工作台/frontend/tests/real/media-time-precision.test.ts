import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from 'vitest';
import { ensureNodeTimeline, getClipEndTime } from '@/app/_features/node-timeline/schema';

test('真实Wan媒体的毫秒裁切经过连续保存保持有效，片段顺序与互动时间保留', () => {
  const product = path.resolve('..');
  const manifest = JSON.parse(readFileSync(path.join(product, '.intermediate/media-sequence-smoke-20261116/browser-media.json'), 'utf8'));
  const durations = manifest.videos.map((item: { path: string }) => {
    const probe = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'json', path.join(product, item.path)], { encoding: 'utf8' }));
    return Math.floor(Number(probe.format.duration) * 1000) / 1000;
  });
  let time = 0;
  const clips = durations.map((duration: number, index: number) => {
    const clip = { id: `actual-${index}`, type: 'video' as const, startTime: time, duration, sourceStart: 0, sourceDuration: duration, src: manifest.videos[index].path, enabled: true };
    time += duration;
    return clip;
  });
  const timeline = ensureNodeTimeline({ version: 2, duration: time, tracks: [{ id: 'media', type: 'media', name: '真实视频', clips }], bookmarks: [] });
  const saved = ensureNodeTimeline(timeline);
  expect(saved).toEqual(timeline);
  for (const [index, clip] of saved.tracks[0].clips.entries()) {
    expect(clip.duration).toBe(durations[index]);
    expect(clip.sourceDuration).toBe(durations[index]);
    if (index) expect(clip.startTime).toBe(getClipEndTime(saved.tracks[0].clips[index - 1]));
  }
});
