import { z } from 'zod';

import { api } from './cloudApi';

const publicationSchema = z.object({
  slug: z.string().regex(/^[a-zA-Z0-9_-]+$/),
  title: z.string().min(1),
  description: z.string(),
  posterUrl: z.string().nullable(),
  trailerUrl: z.string().nullable(),
  createdAt: z.string(),
}).refine(({ slug, posterUrl, trailerUrl }) => {
  const base = '/api/v1/publications/' + slug;
  return (posterUrl === null || posterUrl === base + '/media/poster') &&
    (trailerUrl === null || trailerUrl === base + '/media/trailer');
}, '作品媒体地址无效');

const playablePublicationSchema = z.intersection(publicationSchema, z.object({
  playUrl: z.string(),
})).refine(({ slug, playUrl }) => playUrl === '/api/v1/publications/' + slug + '/play/index.html', '作品播放地址无效');

export type PublicWork = z.infer<typeof publicationSchema>;
export type PlayableWork = z.infer<typeof playablePublicationSchema>;

export async function listPublicWorks(signal: AbortSignal): Promise<PublicWork[]> {
  const result = await api('/publications', { signal });
  return z.object({ publications: z.array(publicationSchema) }).parse(result).publications;
}

export async function getPublicWork(slug: string, signal: AbortSignal): Promise<PlayableWork> {
  const result = await api('/publications/' + encodeURIComponent(slug), { signal });
  const { publication } = z.object({ publication: playablePublicationSchema }).parse(result);
  if (publication.slug !== slug) throw new Error('作品地址与当前页面不符');
  return publication;
}
