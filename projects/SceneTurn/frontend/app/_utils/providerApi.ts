import { z } from 'zod';
import { api } from './cloudApi';

export const modelKinds = ['text', 'image', 'video'] as const;
export type ModelKind = typeof modelKinds[number];
export const modelKindLabels: Record<ModelKind, string> = { text: '文本', image: '图片', video: '视频' };

const defaultsSchema = z.object({ text: z.string(), image: z.string(), video: z.string() });
const priceSchema = z.object({
  version: z.string(), verified: z.literal(true), currency: z.literal('CNY'),
  cacheHitMicrosPerMillion: z.number().nullish(), cacheMissMicrosPerMillion: z.number().nullish(), outputMicrosPerMillion: z.number().nullish(),
  microsPerImage: z.number().nullish(), centsPerSecond: z.record(z.string(), z.number()).nullish(),
});
const modelSchema = z.object({
  id: z.string(), providerId: z.string(), label: z.string(), kind: z.enum(modelKinds), model: z.string(),
  configured: z.boolean(), available: z.boolean(), unavailableReason: z.string().nullish(), requiresEntitlement: z.boolean(),
  capabilities: z.object({
    firstFrame: z.boolean().optional(), lastFrame: z.boolean().optional(), audio: z.boolean().optional(),
    minDuration: z.number().optional(), maxDuration: z.number().optional(), resolutions: z.array(z.string()).optional(),
    maxReferences: z.number().optional(), minDimension: z.number().optional(), maxDimension: z.number().optional(),
    minPixels: z.number().optional(), maxPixels: z.number().optional(),
    ratios: z.array(z.string()).optional(), fixedFrameRatioMode: z.literal('adaptive').optional(),
    videoInputModes: z.array(z.enum(['reference', 'edit', 'extend'])).optional(),
    maxReferenceImages: z.number().int().positive().optional(),
    localConnection: z.boolean().optional(),
  }),
});
const connectionSchema = z.object({
  providerId: z.string(), configured: z.boolean(), revision: z.number(), canManage: z.boolean(),
  region: z.string(), workspaceId: z.string().nullish(), entitledModelIds: z.array(z.string()), prices: z.record(z.string(), priceSchema),
});
const catalogSchema = z.object({
  models: z.array(modelSchema), connections: z.array(connectionSchema), defaults: defaultsSchema, revision: z.number().nullish(),
});

export type ProviderCatalog = z.infer<typeof catalogSchema>;
export type ModelProfile = z.infer<typeof modelSchema>;
export type ProviderConnection = z.infer<typeof connectionSchema>;
export type ProviderPrice = z.infer<typeof priceSchema>;
export type ModelDefaults = z.infer<typeof defaultsSchema>;

export async function getProviderCatalog(projectId?: string) {
  const query = projectId ? `?projectId=${encodeURIComponent(projectId)}` : '';
  return catalogSchema.parse(await api(`/settings/model-providers${query}`));
}

export async function saveProviderConnection(providerId: string, body: {
  expectedRevision: number; apiKey?: string; apiKeys?: string[]; workspaceId?: string; entitledModelIds?: string[];
  prices?: Record<string, ProviderPrice>;
}) {
  return catalogSchema.parse(await api(`/settings/model-providers/${encodeURIComponent(providerId)}`, { method: 'PUT', body: JSON.stringify(body) }));
}

export async function saveModelDefaults(projectId: string, values: ModelDefaults, expectedRevision: number, operationId: string) {
  return z.object({ defaults: defaultsSchema, revision: z.number() }).parse(await api(`/projects/${encodeURIComponent(projectId)}/model-defaults`, {
    method: 'PUT', body: JSON.stringify({ ...values, expectedRevision, operationId }),
  }));
}
