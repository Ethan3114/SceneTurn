import { z } from 'zod';

import type { OpenFMVAsset, OpenFMVProject } from '@/app/_types';
import type { CanvasConnection, CanvasNodeData, ViewportTransform } from '@/app/_features/creation-canvas/upstream/canvas';

export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

const userSchema = z.object({ id: z.string(), email: z.string().email() });
export type CloudUser = z.infer<typeof userSchema>;
export interface CanvasSnapshot {
  sceneId: string;
  nodes: CanvasNodeData[];
  connections: CanvasConnection[];
  viewport: ViewportTransform;
}
export interface ProjectEnvelope { project: OpenFMVProject; revision: number; canvases: CanvasSnapshot[] }
export interface ProjectSummary { id: string; title: string; revision: number; createdAt: string; updatedAt: string }
export interface GenerationView {
  id: string; projectId: string; sceneId: string; sourceNodeId: string; clipId: string;
  projectRevision: number;
  outputNodeId: string | null;
  material?: true;
  canResume?: boolean;
  retryOf?: string | null;
  canRetry?: boolean;
  status: string; input: { prompt?: string; sourcePrompt?: string; inputCompositionVersion?: number; outputNodeId?: string; inputs?: { nodeId: string; type: string; title: string; text?: string; label?: string; assetId?: string; durationMs?: number }[]; duration: number; resolution: string; model?: string; [key: string]: unknown };
  confirmationHash: string; quotedCents?: number; reservedCents?: number; actualCents?: number | null;
  remoteTaskId?: string | null; assetId: string | null; error: { code: string; message: string } | string | null;
  createdAt: string; updatedAt: string;
}
export interface WanSettings { configured: boolean; workspaceId: string; region: string; model: string; prices: Record<string, unknown> }
export interface TextSettings {
  configured: boolean; canManage: boolean; provider: string; model: string; billingPolicyConfigured: boolean;
  prices: { version: string; currency: string; unit: string; peak: { cacheHitYuan: number; cacheMissYuan: number; outputYuan: number }; offPeak: { cacheHitYuan: number; cacheMissYuan: number; outputYuan: number } };
}
export interface TextGenerationView {
  id: string; projectId: string; sceneId: string; sourceNodeId: string; outputNodeId: string;
  task: string; projectRevision: number; status: string;
  input: { sourcePrompt: string; prompt: string; inputs: { nodeId: string; title: string; text: string; label: string }[] };
  result: { text: string } | { shots: { no: number; text: string }[] } | { no: number; text: string } | null;
  outputText: string | null; remoteRequestId: string | null; provider: string; model: string;
  inputTokens: number | null; outputTokens: number | null; totalTokens: number | null;
  cacheHitTokens: number | null; cacheMissTokens: number | null; costMicros: number | null; reservedCostMicros: number;
  usage: Record<string, unknown> | null; error: { code: string; message: string } | string | null;
  appliedRevision: number | null; createdAt: string; updatedAt: string;
}
export interface ImageSplitParams { rows: number; columns: number; horizontalLines?: number[]; verticalLines?: number[] }

const snapshots = new Map<string, OpenFMVProject>();
let activeProjectId: string | null = null;
export const cachedCloudProjects = () => [...snapshots.values()];
export const activeCloudProjectId = () => activeProjectId;
export const clearCloudProjects = () => { snapshots.clear(); activeProjectId = null; };
export const acceptProjectEnvelope = (value: ProjectEnvelope, activate = true) => {
  const project = { ...value.project, metadata: { ...value.project.metadata, serverRevision: value.revision } };
  snapshots.set(project.id, project);
  if (activate) activeProjectId = project.id;
  return { ...value, project };
};

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...options.headers },
  });
  if (!response.headers.get('content-type')?.includes('application/json')) {
    throw new ApiError('SERVICE_RESPONSE_INVALID', '项目服务没有返回有效响应，请检查服务状态。', response.status);
  }
  const payload = await response.json();
  if (!response.ok) {
    const problem = z.object({ error: z.object({ code: z.string(), message: z.string() }) }).parse(payload);
    throw new ApiError(problem.error.code, problem.error.message, response.status);
  }
  return payload as T;
}

export const operationId = () => crypto.randomUUID();
export async function currentUser() {
  const result = await api<{ user: CloudUser }>('/auth/me');
  return userSchema.parse(result.user);
}
export async function authenticate(mode: 'login' | 'register', email: string, password: string) {
  const result = await api<{ user: CloudUser }>(`/auth/${mode}`, { method: 'POST', body: JSON.stringify({ email, password }) });
  clearCloudProjects();
  return userSchema.parse(result.user);
}
export async function logout() {
  await api('/auth/logout', { method: 'POST' });
  clearCloudProjects();
}
export const projectList = () => api<{ projects: ProjectSummary[] }>('/projects');
export async function refreshCloudProjects() {
  const { projects } = await projectList();
  return Promise.all(projects.map(async ({ id }) => acceptProjectEnvelope(await api<ProjectEnvelope>(`/projects/${encodeURIComponent(id)}`), false).project));
}
export async function getCloudProject(id: string) {
  return acceptProjectEnvelope(await api<ProjectEnvelope>(`/projects/${encodeURIComponent(id)}`));
}
export async function createCloudProject(title: string) {
  return acceptProjectEnvelope(await api<ProjectEnvelope>('/projects', { method: 'POST', body: JSON.stringify({ title }) }));
}
export async function saveCloudProject(project: OpenFMVProject) {
  const expectedRevision = project.metadata.serverRevision;
  if (expectedRevision === undefined) throw new Error('项目尚未取得服务端版本，请重新打开项目。');
  return acceptProjectEnvelope(await api<ProjectEnvelope>(`/projects/${project.id}`, {
    method: 'PUT', body: JSON.stringify({ expectedRevision, operationId: operationId(), project }),
  }));
}
export async function createCloudScene(id: string, title: string, expectedRevision: number) {
  return acceptProjectEnvelope(await api<ProjectEnvelope>(`/projects/${id}/scenes`, {
    method: 'POST', body: JSON.stringify({ title, expectedRevision, operationId: operationId() }),
  }));
}
export const getCloudCanvas = (id: string, sceneId: string) => api<{ canvas: CanvasSnapshot; revision: number }>(`/projects/${id}/canvases/${sceneId}`);
export const saveCloudCanvas = (id: string, canvas: CanvasSnapshot, expectedRevision: number) => api<{ canvas: CanvasSnapshot; revision: number }>(`/projects/${id}/canvases/${canvas.sceneId}`, {
  method: 'PUT', body: JSON.stringify({ expectedRevision, operationId: operationId(), nodes: canvas.nodes, connections: canvas.connections, viewport: canvas.viewport }),
});
export const getWanSettings = () => api<WanSettings>('/settings/wan');
export const getTextSettings = () => api<TextSettings>('/settings/text');
export const saveTextSettings = (settings: { apiKey: string; provider: 'deepseek'; model: 'deepseek-flash' }) => api<TextSettings>('/settings/text', { method: 'PUT', body: JSON.stringify(settings) });
export const saveWanSettings = (settings: { workspaceId: string; apiKey: string; region: string; model: string }) => api<WanSettings>('/settings/wan', { method: 'PUT', body: JSON.stringify(settings) });
export const getGenerations = (id: string) => api<{ generations: GenerationView[] }>(`/projects/${id}/generations`);
export const getTextGenerations = (id: string) => api<{ textGenerations: TextGenerationView[] }>(`/projects/${id}/text-generations`);
export const getTextGeneration = (id: string) => api<{ textGeneration: TextGenerationView }>(`/text-generations/${id}`);
export const createTextGeneration = (id: string, input: { sceneId: string; sourceNodeId: string; outputNodeId: string; prompt: string; task: string; expectedRevision: number; operationId: string; modelProfileId?: string }) => api<{ textGeneration: TextGenerationView }>(`/projects/${id}/text-generations`, { method: 'POST', body: JSON.stringify(input) });
export const stopTextGeneration = (id: string) => api<{ textGeneration: TextGenerationView }>(`/text-generations/${id}/stop`, { method: 'POST', body: '{}' });
export const resumeTextGeneration = (id: string) => api<{ textGeneration: TextGenerationView }>(`/text-generations/${id}/resume`, { method: 'POST', body: '{}' });
export const applyTextGeneration = (id: string, expectedRevision: number) => api<{ textGeneration: TextGenerationView }>(`/text-generations/${id}/apply`, { method: 'POST', body: JSON.stringify({ expectedRevision, operationId: operationId() }) });
export const joinGeneration = (id: string, input: { sceneId: string; sourceNodeId: string; outputNodeId: string; expectedRevision: number; operationId: string }) => api<{ generation: GenerationView }>(`/projects/${id}/generations/join`, { method: 'POST', body: JSON.stringify(input) });
export const splitCloudImage = (id: string, input: ImageSplitParams & { sceneId: string; sourceNodeId: string; expectedRevision: number; operationId: string }) => api<ProjectEnvelope & { pieces: { nodeId: string; assetId: string; row: number; column: number }[] }>(`/projects/${id}/media-tools/split`, { method: 'POST', body: JSON.stringify(input) });
export const quoteGeneration = (id: string, input: { sceneId: string; sourceNodeId: string; outputNodeId?: string; clipId?: string; prompt: string; duration: number; resolution: string; operationId: string; modelProfileId?: string; ratio?: string; firstFrameNodeId?: string; lastFrameNodeId?: string; referenceVideoNodeId?: string; referenceImageNodeIds?: string[]; videoInputMode?: 'text' | 'reference' | 'edit' | 'extend' }) => api<{ generation: GenerationView }>(`/projects/${id}/generations/quote`, { method: 'POST', body: JSON.stringify(input) });
export const confirmGeneration = (task: GenerationView, expectedRevision: number) => api<{ generation: GenerationView }>(`/generations/${task.id}/confirm`, { method: 'POST', body: JSON.stringify({ confirmationHash: task.confirmationHash, expectedRevision }) });
export const quoteGenerationRetry = (id: string, expectedRevision: number) => api<{ generation: GenerationView }>(`/generations/${id}/retry-quote`, { method: 'POST', body: JSON.stringify({ expectedRevision }) });
export const resumeGeneration = (id: string) => api<{ generation: GenerationView }>(`/generations/${id}/resume`, { method: 'POST', body: '{}' });
export async function adoptGeneration(id: string, expectedRevision: number) {
  return acceptProjectEnvelope(await api<ProjectEnvelope>(`/generations/${id}/adopt`, { method: 'POST', body: JSON.stringify({ expectedRevision, operationId: operationId() }) }));
}
export const assetContentUrl = (id: string) => `/api/v1/assets/${encodeURIComponent(id)}/content`;
export async function addAssetToSceneTimeline(id: string, sceneId: string, assetId: string, expectedRevision: number) {
  return acceptProjectEnvelope(await api<ProjectEnvelope>(`/projects/${id}/timeline-assets`, { method: 'POST', body: JSON.stringify({ sceneId, assetId, expectedRevision, operationId: operationId() }) }));
}
export async function uploadCloudAsset(id: string, file: File): Promise<OpenFMVAsset> {
  const previousRevision = snapshots.get(id)?.metadata.serverRevision;
  const form = new FormData();
  form.append('file', file);
  const result = await api<ProjectEnvelope & { asset: Record<string, unknown> }>(`/projects/${id}/assets`, { method: 'POST', body: form });
  const { asset } = result;
  const saved = acceptProjectEnvelope(result).project;
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('openfmv:assets-saved', { detail: { project: saved, previousRevision } }));
  const storedAsset = saved.assets.find((item) => item.id === asset.id);
  if (!storedAsset) throw new Error('服务端未登记上传素材，请检查项目状态。');
  return storedAsset;
}
