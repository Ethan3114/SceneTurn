// Generated from shared/ipc-contract.ts. Run npm run ipc:build after editing the source.
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.registerIpcHandler = exports.createSceneTurnBridge = exports.validateIpcResult = exports.validateIpcArgs = exports.sceneTurnBridgeMethods = exports.sceneTurnIpcContract = void 0;
const zod_1 = require("zod");
const { agentDefinitions, byokProviderDefinitions, mediaProviderDefinitions, } = require('./ai-definitions');
const nonEmptyString = zod_1.z.string().min(1);
const recordSchema = zod_1.z.record(zod_1.z.string(), zod_1.z.unknown());
const enumFrom = (values) => zod_1.z.enum(values);
const agentIdSchema = enumFrom(agentDefinitions.map((agent) => agent.id));
const byokProviderIdSchema = enumFrom(byokProviderDefinitions.map((provider) => provider.id));
const mediaProviderIdSchema = enumFrom(mediaProviderDefinitions.map((provider) => provider.id));
const assetSchema = zod_1.z.object({
    id: nonEmptyString,
    type: enumFrom(['image', 'video', 'audio', 'text']),
    name: zod_1.z.string(),
    path: nonEmptyString,
    relativePath: nonEmptyString,
    importedAt: nonEmptyString,
    metadata: recordSchema.optional(),
});
const projectSchema = zod_1.z.object({
    schemaVersion: zod_1.z.literal(1),
    id: nonEmptyString,
    title: nonEmptyString,
    graphData: zod_1.z.object({
        nodes: zod_1.z.array(zod_1.z.unknown()),
        edges: zod_1.z.array(zod_1.z.unknown()),
    }),
    assets: zod_1.z.array(assetSchema),
    metadata: recordSchema,
    createdAt: nonEmptyString,
    updatedAt: nonEmptyString,
});
const gameExportConfigSchema = zod_1.z.object({
    gameTitle: nonEmptyString,
    outputDirectory: nonEmptyString,
    locale: zod_1.z.string().optional(),
    entryNodeId: zod_1.z.string().optional(),
    windowMode: enumFrom(['windowed', 'fullscreen', 'borderless']),
    resolution: zod_1.z.object({
        width: zod_1.z.number().int().positive(),
        height: zod_1.z.number().int().positive(),
    }),
    includeDebugOverlay: zod_1.z.boolean(),
});
const cliSelectionSchema = zod_1.z.object({
    agentId: agentIdSchema,
    model: zod_1.z.string(),
    reasoningEffort: zod_1.z.string().optional(),
});
const byokProviderConfigSchema = zod_1.z.object({
    providerId: byokProviderIdSchema,
    apiKey: zod_1.z.string(),
    baseUrl: zod_1.z.string(),
    model: zod_1.z.string(),
});
const mediaProviderConfigSchema = zod_1.z.object({
    providerId: mediaProviderIdSchema,
    apiKey: zod_1.z.string(),
    baseUrl: zod_1.z.string(),
    model: zod_1.z.string(),
});
const aiConfigSchema = zod_1.z.object({
    executionMode: zod_1.z.literal('cli'),
    selectedCliAgentId: agentIdSchema,
    cliSelections: zod_1.z.array(cliSelectionSchema),
    selectedByokProviderId: byokProviderIdSchema,
    byokProviders: zod_1.z.array(byokProviderConfigSchema),
    mediaProviders: zod_1.z.array(mediaProviderConfigSchema),
});
const agentInfoSchema = zod_1.z.object({
    id: agentIdSchema,
    name: zod_1.z.string(),
    bin: zod_1.z.string(),
    version: zod_1.z.string(),
    available: zod_1.z.boolean(),
    models: zod_1.z.array(zod_1.z.string()),
    reasoningOptions: zod_1.z.array(zod_1.z.string()).optional(),
});
const connectionTestResultSchema = zod_1.z.object({
    ok: zod_1.z.boolean(),
    message: zod_1.z.string(),
});
const chatAttachmentSchema = zod_1.z.object({
    name: zod_1.z.string(),
    type: zod_1.z.string(),
    size: zod_1.z.number(),
    content: zod_1.z.string().optional(),
    truncated: zod_1.z.boolean().optional(),
});
const chatRequestSchema = zod_1.z.object({
    messages: zod_1.z.array(zod_1.z.object({
        role: enumFrom(['user', 'assistant']),
        content: zod_1.z.string(),
        attachments: zod_1.z.array(chatAttachmentSchema).optional(),
    })),
});
const chatResponseSchema = zod_1.z.object({
    ok: zod_1.z.boolean(),
    content: zod_1.z.string(),
    agentId: agentIdSchema,
    model: zod_1.z.string(),
    error: zod_1.z.string().optional(),
});
const outputDirectoryResultSchema = zod_1.z.object({ outputDirectory: nonEmptyString });
exports.sceneTurnIpcContract = {
    openProject: {
        channel: 'sceneturn:open-project',
        args: zod_1.z.tuple([]),
        result: projectSchema.nullable(),
    },
    saveProject: {
        channel: 'sceneturn:save-project',
        args: zod_1.z.tuple([projectSchema]),
        result: projectSchema,
    },
    importAsset: {
        channel: 'sceneturn:import-asset',
        args: zod_1.z.tuple([nonEmptyString]),
        result: assetSchema,
    },
    selectAsset: {
        channel: 'sceneturn:select-asset',
        args: zod_1.z.tuple([]),
        result: assetSchema.nullable(),
    },
    exportGame: {
        channel: 'sceneturn:export-game',
        args: zod_1.z.tuple([projectSchema, gameExportConfigSchema]),
        result: outputDirectoryResultSchema,
    },
    selectDirectory: {
        channel: 'sceneturn:select-directory',
        args: zod_1.z.tuple([]),
        result: zod_1.z.string().nullable(),
    },
    minimizeWindow: {
        channel: 'sceneturn:minimize-window',
        args: zod_1.z.tuple([]),
        result: zod_1.z.void(),
    },
    toggleMaximizeWindow: {
        channel: 'sceneturn:toggle-maximize-window',
        args: zod_1.z.tuple([]),
        result: zod_1.z.void(),
    },
    closeWindow: {
        channel: 'sceneturn:close-window',
        args: zod_1.z.tuple([]),
        result: zod_1.z.void(),
    },
    getAiConfig: {
        channel: 'sceneturn:get-ai-config',
        args: zod_1.z.tuple([]),
        result: aiConfigSchema,
    },
    saveAiConfig: {
        channel: 'sceneturn:save-ai-config',
        args: zod_1.z.tuple([aiConfigSchema]),
        result: aiConfigSchema,
    },
    detectAiAgents: {
        channel: 'sceneturn:detect-ai-agents',
        args: zod_1.z.tuple([]),
        result: zod_1.z.array(agentInfoSchema),
    },
    testAiAgent: {
        channel: 'sceneturn:test-ai-agent',
        args: zod_1.z.tuple([agentIdSchema]),
        result: connectionTestResultSchema,
    },
    sendChatMessage: {
        channel: 'sceneturn:send-chat-message',
        args: zod_1.z.tuple([chatRequestSchema]),
        result: chatResponseSchema,
    },
    testByokProvider: {
        channel: 'sceneturn:test-byok-provider',
        args: zod_1.z.tuple([byokProviderConfigSchema]),
        result: connectionTestResultSchema,
    },
    testMediaProvider: {
        channel: 'sceneturn:test-media-provider',
        args: zod_1.z.tuple([mediaProviderConfigSchema]),
        result: connectionTestResultSchema,
    },
};
exports.sceneTurnBridgeMethods = Object.freeze(Object.keys(exports.sceneTurnIpcContract));
const formatValidationIssues = (issues) => issues
    .map((issue) => `${issue.path.join('.') || '(root)'}: ${issue.message}`)
    .join('; ');
const getContractEntry = (method) => {
    return exports.sceneTurnIpcContract[method];
};
const validateIpcArgs = (method, args) => {
    const entry = getContractEntry(method);
    const result = entry.args.safeParse(args);
    if (!result.success) {
        throw new TypeError(`Invalid SceneTurn IPC args for ${method}: ${formatValidationIssues(result.error.issues)}`);
    }
    return result.data;
};
exports.validateIpcArgs = validateIpcArgs;
const validateIpcResult = (method, value) => {
    const entry = getContractEntry(method);
    const result = entry.result.safeParse(value);
    if (!result.success) {
        throw new TypeError(`Invalid SceneTurn IPC result for ${method}: ${formatValidationIssues(result.error.issues)}`);
    }
    return result.data;
};
exports.validateIpcResult = validateIpcResult;
const createSceneTurnBridge = (invoke) => {
    return Object.fromEntries(exports.sceneTurnBridgeMethods.map((method) => {
        const { channel } = exports.sceneTurnIpcContract[method];
        return [method, (...args) => Promise.resolve().then(async () => {
                const parsedArgs = (0, exports.validateIpcArgs)(method, args);
                const result = await invoke(channel, ...parsedArgs);
                return (0, exports.validateIpcResult)(method, result);
            })];
    }));
};
exports.createSceneTurnBridge = createSceneTurnBridge;
const registerIpcHandler = (ipcMain, method, handler) => {
    const { channel } = getContractEntry(method);
    ipcMain.handle(channel, async (event, ...args) => {
        const parsedArgs = (0, exports.validateIpcArgs)(method, args);
        const result = await handler(event, ...parsedArgs);
        return (0, exports.validateIpcResult)(method, result);
    });
};
exports.registerIpcHandler = registerIpcHandler;
