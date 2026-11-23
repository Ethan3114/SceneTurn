import { z } from 'zod';
import { api } from './cloudApi';

const jobSchema = z.object({
  id: z.string(), projectId: z.string(), prompt: z.string(), status: z.enum([
    'awaiting_confirmation', 'queued', 'running', 'waiting_connection', 'submission_unknown', 'ready', 'failed', 'stopped',
  ]),
  asset: z.object({ id: z.string(), name: z.string(), url: z.string(), width: z.number().nullable(), height: z.number().nullable() }).nullable(),
  error: z.object({ code: z.string(), message: z.string() }).nullable(),
  referenceAssets: z.array(z.object({ name: z.string() })).optional(),
});

export type CodexImageJob = z.infer<typeof jobSchema>;
const path = (projectId: string) => `/projects/${encodeURIComponent(projectId)}/codex-images`;

export async function listCodexImages(projectId: string) {
  return z.object({ jobs: z.array(jobSchema) }).parse(await api(path(projectId))).jobs;
}

export async function createCodexImage(projectId: string, body: { operationId: string; expectedRevision: number; prompt: string; referenceAssetIds: string[]; transparentBackground: boolean }) {
  return z.object({ job: jobSchema }).parse(await api(path(projectId), { method: 'POST', body: JSON.stringify(body) })).job;
}

export async function actOnCodexImage(projectId: string, jobId: string, action: 'confirm' | 'stop', operationId: string, connectionId?: string) {
  const body = action === 'confirm' ? { operationId, connectionId, accountUsageConfirmed: true } : { operationId };
  return z.object({ job: jobSchema }).parse(await api(`${path(projectId)}/${encodeURIComponent(jobId)}/${action}`, { method: 'POST', body: JSON.stringify(body) })).job;
}
