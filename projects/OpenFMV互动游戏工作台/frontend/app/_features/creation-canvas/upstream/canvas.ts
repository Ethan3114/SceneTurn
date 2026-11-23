export type Position = {
    x: number;
    y: number;
};

export type ViewportTransform = {
    x: number;
    y: number;
    k: number;
};

export enum CanvasNodeType {
    Image = "image",
    Text = "text",
    Config = "config",
    Video = "video",
    Audio = "audio",
    Group = "group",
}

// Node types are open strings: built-ins use CanvasNodeType and plugins use "<pluginId>:<name>".
export type CanvasNodeTypeId = CanvasNodeType | (string & {});

export type CanvasNodeStatus = "idle" | "success" | "loading" | "error";
export type CanvasGenerationMode = "text" | "image" | "video" | "audio";
export type CanvasImageGenerationType = "generation" | "edit";

export type CanvasNodeImage = {
    id: string;
    status: CanvasNodeStatus;
    errorDetails?: string;
    content: string;
    storageKey?: string;
    naturalWidth: number;
    naturalHeight: number;
    bytes: number;
    mimeType: string;
};

export type CanvasNodeText = {
    id: string;
    status: CanvasNodeStatus;
    errorDetails?: string;
    content: string;
};

export type CanvasNodeMetadata = {
    productionTool?: 'director' | 'audio-edit';
    audioMode?: 'music';
    audioSettings?: import('../production/media-edit').AudioSettings;
    editSettings?: import('../production/media-edit').EditSettings;
    directorScene?: import('../production/director-scene').DirectorScene;
    characterDesign?: { name: string; face: string; body: string; outfit: string; notes: string; reference: string; authorized: boolean };
    scriptVersion?: "current" | "legacy";
    filmGuidance?: Partial<Record<'style' | 'palette' | 'composition' | 'rhythm' | 'camera' | 'lens' | 'focal' | 'aperture', string>>;
    scriptRows?: { id: string; duration: number | null; title: string; visual: string; action: string; dialogue: string }[];
    publicMaterialRole?: string;
    creativeArtifact?: Record<string, unknown>;
    agentWorkspace?: "project" | "scene";
    agentRole?: string;
    agentGoal?: string;
    agentExecutor?: "platform" | "codex";
    agentDeliveryMode?: "professional" | "text_edit" | "tool_action";
    agentBranchId?: string;
    agentTargetIds?: string[];
    content?: string;
    textGenerationId?: string;
    result?: Record<string, unknown>;
    composerContent?: string;
    prompt?: string;
    status?: CanvasNodeStatus;
    errorDetails?: string;
    fontSize?: number;
    generationMode?: CanvasGenerationMode;
    dramaTask?: string;
    ecommercePurposes?: string;
    ecommerceSizes?: string;
    ecommercePurpose?: string;
    imageWatermark?: string;
    promptExtend?: string;
    pendingResult?: boolean;
    outputNodeId?: string;
    plotSpawnOf?: string;
    generationType?: CanvasImageGenerationType;
    model?: string;
    modelProfileId?: string;
    reasoningEffort?: "auto" | "low" | "medium" | "high" | "xhigh";
    size?: string;
    imageWidth?: number;
    imageHeight?: number;
    quality?: string;
    background?: string;
    count?: number;
    textCount?: number;
    texts?: CanvasNodeText[];
    primaryTextId?: string;
    seconds?: string;
    vquality?: string;
    videoSourceMode?: "text" | "frames" | "reference" | "edit" | "extend";
    firstFrameNodeId?: string;
    lastFrameNodeId?: string;
    referenceVideoNodeId?: string;
    videoReferenceImageNodeIds?: string[];
    videoRatio?: string;
    generateAudio?: string;
    watermark?: string;
    videoMode?: string;
    audioVoice?: string;
    audioFormat?: string;
    audioSpeed?: string;
    audioInstructions?: string;
    references?: string[];
    naturalWidth?: number;
    naturalHeight?: number;
    freeResize?: boolean;
    images?: CanvasNodeImage[];
    primaryImageId?: string;
    storageKey?: string;
    mimeType?: string;
    bytes?: number;
    durationMs?: number;
    videoTaskId?: string;
    videoTaskProvider?: "openai" | "gemini";
    groupId?: string;
    interactive?: boolean; // Plugin node interaction/move state; see CanvasNodeDefinition.interactionToggle.
};

export type CanvasNodeData = {
    id: string;
    type: CanvasNodeTypeId;
    title: string;
    position: Position;
    width: number;
    height: number;
    metadata?: CanvasNodeMetadata;
};

export type CanvasConnection = {
    id: string;
    fromNodeId: string;
    toNodeId: string;
};

export type CanvasAssistantReference = {
    id: string;
    type: CanvasNodeTypeId;
    title: string;
    dataUrl?: string;
    storageKey?: string;
    text?: string;
};

export type CanvasAssistantImage = {
    id: string;
    dataUrl: string;
    storageKey?: string;
    prompt: string;
};

export type CanvasAssistantMessage = {
    id: string;
    role: "user" | "assistant" | "system" | "tool" | "error";
    title?: string;
    text: string;
    meta?: string;
    detail?: unknown;
    references?: CanvasAssistantReference[];
};

export type CanvasAssistantSession = {
    id: string;
    title: string;
    messages: CanvasAssistantMessage[];
    createdAt: string;
    updatedAt: string;
};

export type ConnectionHandle = {
    nodeId: string;
    handleType: "source" | "target";
};

export type SelectionBox = {
    startWorldX: number;
    startWorldY: number;
    currentWorldX: number;
    currentWorldY: number;
    additive: boolean;
    initialSelectedNodeIds: string[];
};

export type ContextMenuState =
    | {
          type: "node";
          x: number;
          y: number;
          nodeId: string;
      }
    | {
          type: "connection";
          x: number;
          y: number;
          connectionId: string;
      };
