import { z } from 'zod';

import type {
  GameExportConfig,
  SceneTurnAgentId,
  SceneTurnAgentInfo,
  SceneTurnAiConfig,
  SceneTurnAsset,
  SceneTurnByokProviderConfig,
  SceneTurnChatAttachment,
  SceneTurnChatRequest,
  SceneTurnChatResponse,
  SceneTurnConnectionTestResult,
  SceneTurnMediaProviderConfig,
  SceneTurnProject,
} from '../app/_types';

const {
  agentDefinitions,
  byokProviderDefinitions,
  mediaProviderDefinitions,
} = require('./ai-definitions') as {
  agentDefinitions: Array<{ id: SceneTurnAgentId }>;
  byokProviderDefinitions: Array<{ id: SceneTurnByokProviderConfig['providerId'] }>;
  mediaProviderDefinitions: Array<{ id: SceneTurnMediaProviderConfig['providerId'] }>;
};

const nonEmptyString = z.string().min(1);
const recordSchema = z.record(z.string(), z.unknown());

const enumFrom = <T extends string>(values: T[]) => z.enum(values as [T, ...T[]]);

const agentIdSchema = enumFrom(agentDefinitions.map((agent) => agent.id));
const byokProviderIdSchema = enumFrom(byokProviderDefinitions.map((provider) => provider.id));
const mediaProviderIdSchema = enumFrom(mediaProviderDefinitions.map((provider) => provider.id));

const assetSchema: z.ZodType<SceneTurnAsset> = z.object({
  id: nonEmptyString,
  type: enumFrom(['image', 'video', 'audio', 'text']),
  name: z.string(),
  path: nonEmptyString,
  relativePath: nonEmptyString,
  importedAt: nonEmptyString,
  metadata: recordSchema.optional(),
});

const projectSchema: z.ZodType<SceneTurnProject> = z.object({
  schemaVersion: z.literal(1),
  id: nonEmptyString,
  title: nonEmptyString,
  graphData: z.object({
    nodes: z.array(z.unknown()),
    edges: z.array(z.unknown()),
  }) as z.ZodType<SceneTurnProject['graphData']>,
  assets: z.array(assetSchema),
  metadata: recordSchema as z.ZodType<SceneTurnProject['metadata']>,
  createdAt: nonEmptyString,
  updatedAt: nonEmptyString,
});

const gameExportConfigSchema: z.ZodType<GameExportConfig> = z.object({
  gameTitle: nonEmptyString,
  outputDirectory: nonEmptyString,
  locale: z.string().optional(),
  entryNodeId: z.string().optional(),
  windowMode: enumFrom(['windowed', 'fullscreen', 'borderless']),
  resolution: z.object({
    width: z.number().int().positive(),
    height: z.number().int().positive(),
  }),
  includeDebugOverlay: z.boolean(),
});

const cliSelectionSchema: z.ZodType<SceneTurnAiConfig['cliSelections'][number]> = z.object({
  agentId: agentIdSchema,
  model: z.string(),
  reasoningEffort: z.string().optional(),
});

const byokProviderConfigSchema: z.ZodType<SceneTurnByokProviderConfig> = z.object({
  providerId: byokProviderIdSchema,
  apiKey: z.string(),
  baseUrl: z.string(),
  model: z.string(),
});

const mediaProviderConfigSchema: z.ZodType<SceneTurnMediaProviderConfig> = z.object({
  providerId: mediaProviderIdSchema,
  apiKey: z.string(),
  baseUrl: z.string(),
  model: z.string(),
});

const aiConfigSchema: z.ZodType<SceneTurnAiConfig> = z.object({
  executionMode: z.literal('cli'),
  selectedCliAgentId: agentIdSchema,
  cliSelections: z.array(cliSelectionSchema),
  selectedByokProviderId: byokProviderIdSchema,
  byokProviders: z.array(byokProviderConfigSchema),
  mediaProviders: z.array(mediaProviderConfigSchema),
});

const agentInfoSchema: z.ZodType<SceneTurnAgentInfo> = z.object({
  id: agentIdSchema,
  name: z.string(),
  bin: z.string(),
  version: z.string(),
  available: z.boolean(),
  models: z.array(z.string()),
  reasoningOptions: z.array(z.string()).optional(),
});

const connectionTestResultSchema: z.ZodType<SceneTurnConnectionTestResult> = z.object({
  ok: z.boolean(),
  message: z.string(),
});

const chatAttachmentSchema: z.ZodType<SceneTurnChatAttachment> = z.object({
  name: z.string(),
  type: z.string(),
  size: z.number(),
  content: z.string().optional(),
  truncated: z.boolean().optional(),
});

const chatRequestSchema: z.ZodType<SceneTurnChatRequest> = z.object({
  messages: z.array(z.object({
    role: enumFrom(['user', 'assistant']),
    content: z.string(),
    attachments: z.array(chatAttachmentSchema).optional(),
  })),
});

const chatResponseSchema: z.ZodType<SceneTurnChatResponse> = z.object({
  ok: z.boolean(),
  content: z.string(),
  agentId: agentIdSchema,
  model: z.string(),
  error: z.string().optional(),
});

const outputDirectoryResultSchema = z.object({ outputDirectory: nonEmptyString });

export const sceneTurnIpcContract = {
  openProject: {
    channel: 'sceneturn:open-project',
    args: z.tuple([]),
    result: projectSchema.nullable(),
  },
  saveProject: {
    channel: 'sceneturn:save-project',
    args: z.tuple([projectSchema]),
    result: projectSchema,
  },
  importAsset: {
    channel: 'sceneturn:import-asset',
    args: z.tuple([nonEmptyString]),
    result: assetSchema,
  },
  selectAsset: {
    channel: 'sceneturn:select-asset',
    args: z.tuple([]),
    result: assetSchema.nullable(),
  },
  exportGame: {
    channel: 'sceneturn:export-game',
    args: z.tuple([projectSchema, gameExportConfigSchema]),
    result: outputDirectoryResultSchema,
  },
  selectDirectory: {
    channel: 'sceneturn:select-directory',
    args: z.tuple([]),
    result: z.string().nullable(),
  },
  minimizeWindow: {
    channel: 'sceneturn:minimize-window',
    args: z.tuple([]),
    result: z.void(),
  },
  toggleMaximizeWindow: {
    channel: 'sceneturn:toggle-maximize-window',
    args: z.tuple([]),
    result: z.void(),
  },
  closeWindow: {
    channel: 'sceneturn:close-window',
    args: z.tuple([]),
    result: z.void(),
  },
  getAiConfig: {
    channel: 'sceneturn:get-ai-config',
    args: z.tuple([]),
    result: aiConfigSchema,
  },
  saveAiConfig: {
    channel: 'sceneturn:save-ai-config',
    args: z.tuple([aiConfigSchema]),
    result: aiConfigSchema,
  },
  detectAiAgents: {
    channel: 'sceneturn:detect-ai-agents',
    args: z.tuple([]),
    result: z.array(agentInfoSchema),
  },
  testAiAgent: {
    channel: 'sceneturn:test-ai-agent',
    args: z.tuple([agentIdSchema]),
    result: connectionTestResultSchema,
  },
  sendChatMessage: {
    channel: 'sceneturn:send-chat-message',
    args: z.tuple([chatRequestSchema]),
    result: chatResponseSchema,
  },
  testByokProvider: {
    channel: 'sceneturn:test-byok-provider',
    args: z.tuple([byokProviderConfigSchema]),
    result: connectionTestResultSchema,
  },
  testMediaProvider: {
    channel: 'sceneturn:test-media-provider',
    args: z.tuple([mediaProviderConfigSchema]),
    result: connectionTestResultSchema,
  },
} as const;

export type SceneTurnBridgeMethod = keyof typeof sceneTurnIpcContract;
type ContractArgs<Method extends SceneTurnBridgeMethod> = z.output<(typeof sceneTurnIpcContract)[Method]['args']>;
type ContractResult<Method extends SceneTurnBridgeMethod> = z.output<(typeof sceneTurnIpcContract)[Method]['result']>;

export type SceneTurnBridgeMethods = {
  [Method in SceneTurnBridgeMethod]: (...args: ContractArgs<Method>) => Promise<ContractResult<Method>>;
};

export type SceneTurnBridge = SceneTurnBridgeMethods;

export const sceneTurnBridgeMethods = Object.freeze(Object.keys(sceneTurnIpcContract) as SceneTurnBridgeMethod[]);

const formatValidationIssues = (issues: z.core.$ZodIssue[]) => issues
  .map((issue) => `${issue.path.join('.') || '(root)'}: ${issue.message}`)
  .join('; ');

const getContractEntry = <Method extends SceneTurnBridgeMethod>(method: Method) => {
  return sceneTurnIpcContract[method];
};

export const validateIpcArgs = <Method extends SceneTurnBridgeMethod>(
  method: Method,
  args: unknown[]
): ContractArgs<Method> => {
  const entry = getContractEntry(method);
  const result = entry.args.safeParse(args);
  if (!result.success) {
    throw new TypeError(`Invalid SceneTurn IPC args for ${method}: ${formatValidationIssues(result.error.issues)}`);
  }
  return result.data as ContractArgs<Method>;
};

export const validateIpcResult = <Method extends SceneTurnBridgeMethod>(
  method: Method,
  value: unknown
): ContractResult<Method> => {
  const entry = getContractEntry(method);
  const result = entry.result.safeParse(value);
  if (!result.success) {
    throw new TypeError(`Invalid SceneTurn IPC result for ${method}: ${formatValidationIssues(result.error.issues)}`);
  }
  return result.data as ContractResult<Method>;
};

export const createSceneTurnBridge = (
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>
): SceneTurnBridgeMethods => {
  return Object.fromEntries(sceneTurnBridgeMethods.map((method) => {
    const { channel } = sceneTurnIpcContract[method];
    return [method, (...args: unknown[]) => Promise.resolve().then(async () => {
      const parsedArgs = validateIpcArgs(method, args);
      const result = await invoke(channel, ...parsedArgs);
      return validateIpcResult(method, result);
    })];
  })) as SceneTurnBridgeMethods;
};

type IpcMainLike = {
  handle: (channel: string, handler: (event: unknown, ...args: unknown[]) => unknown) => void;
};

export const registerIpcHandler = <Method extends SceneTurnBridgeMethod>(
  ipcMain: IpcMainLike,
  method: Method,
  handler: (event: unknown, ...args: ContractArgs<Method>) => ContractResult<Method> | Promise<ContractResult<Method>>
) => {
  const { channel } = getContractEntry(method);
  ipcMain.handle(channel, async (event, ...args) => {
    const parsedArgs = validateIpcArgs(method, args);
    const result = await handler(event, ...parsedArgs);
    return validateIpcResult(method, result);
  });
};
