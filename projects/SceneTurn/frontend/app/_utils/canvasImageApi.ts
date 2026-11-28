import { z } from 'zod';
import { api } from './cloudApi';

const canvasImageJob = z.object({
  id: z.string(), sceneId: z.string(), sourceNodeId: z.string(), outputNodeId: z.string(),
  modelProfileId: z.string(), model: z.string(), width: z.number(), height: z.number(),
  status: z.enum(['awaiting_confirmation', 'queued', 'claimed', 'submitting', 'response_saved', 'waiting_connection', 'submission_unknown', 'ready', 'failed', 'cancelled']),
  assetId: z.string().nullable(), errorCode: z.string().nullable(), errorMessage: z.string().nullable(), createdAt: z.string(), updatedAt: z.string(),
});

export type CanvasImageJob = z.infer<typeof canvasImageJob>;

const path = (projectId: string) => `/projects/${encodeURIComponent(projectId)}/canvas-image-jobs`;
const jobResponse = z.object({ job: canvasImageJob });

export async function getCanvasImageJobs(projectId: string) {
  return z.object({ jobs: z.array(canvasImageJob) }).parse(await api(path(projectId))).jobs;
}

export async function createCanvasImageJob(projectId: string, body: {
  operationId: string; expectedRevision: number; sceneId: string; sourceNodeId: string; outputNodeId: string;
  modelProfileId: string; prompt: string; width: number; height: number; referenceAssetIds?: string[];
}) {
  return jobResponse.parse(await api(path(projectId), { method: 'POST', body: JSON.stringify(body) })).job;
}

export async function confirmCanvasImageJob(projectId: string, jobId: string, body: { operationId: string; expectedRevision: number; accountUsageConfirmed: true }) {
  return jobResponse.parse(await api(`${path(projectId)}/${encodeURIComponent(jobId)}/confirm`, { method: 'POST', body: JSON.stringify(body) })).job;
}
