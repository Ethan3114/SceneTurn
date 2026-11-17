import { z } from 'zod';
import { api, operationId } from './cloudApi';
import { agentRunSchema } from './agentApi';

const summary = z.object({ id: z.string(), projectId: z.string(), title: z.string(), createdAt: z.string(), updatedAt: z.string() });
const message = z.object({ id: z.string(), role: z.enum(['user', 'assistant']), content: z.string(), context: z.record(z.string(), z.unknown()), runId: z.string().nullable(), createdAt: z.string() });
const conversation = summary.extend({ messages: z.array(message), run: agentRunSchema.nullable() });
export type ProjectConversation = z.infer<typeof conversation>;
export type ConversationSummary = z.infer<typeof summary>;
export type EditingContext = { sceneId?: string; selectedNodeIds?: string[]; selectedClipIds?: string[]; timelineTime?: number; surface: 'editor' | 'timeline' | 'materials' };
export const getConversations = async (projectId: string) => z.object({ conversations: z.array(summary) }).parse(await api(`/projects/${projectId}/conversations`)).conversations;
export const createConversation = async (projectId: string) => z.object({ conversation }).parse(await api(`/projects/${projectId}/conversations`, { method: 'POST', body: JSON.stringify({ operationId: operationId() }) })).conversation;
export const getConversation = async (projectId: string, id: string) => z.object({ conversation }).parse(await api(`/projects/${projectId}/conversations/${id}`)).conversation;
export const getConversationCapabilities = async (projectId: string) => z.object({ tools: z.array(z.string()), skills: z.array(z.string()) }).parse(await api(`/projects/${projectId}/conversation-capabilities`));
export const sendConversationMessage = async (projectId: string, id: string, content: string, expectedRevision: number, context: EditingContext) => z.object({ conversation }).parse(await api(`/projects/${projectId}/conversations/${id}/messages`, { method: 'POST', body: JSON.stringify({ operationId: operationId(), content, expectedRevision, context }) })).conversation;
