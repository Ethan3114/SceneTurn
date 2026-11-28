import { Node, Edge } from '@xyflow/react';

export type NodeType = 'start' | 'scene' | 'end';

export interface SceneTurnGraph {
  nodes: AppNode[];
  edges: AppEdge[];
}

export interface SceneTurnAsset {
  id: string;
  type: 'image' | 'video' | 'audio' | 'text';
  name: string;
  path: string;
  relativePath: string;
  importedAt: string;
  metadata?: Record<string, unknown>;
}

export interface SceneTurnProject {
  schemaVersion: 1;
  id: string;
  title: string;
  graphData: SceneTurnGraph;
  assets: SceneTurnAsset[];
  metadata: {
    description?: string;
    coverImage?: string;
    entryNodeId?: string;
    projectDirectory?: string;
    projectPath?: string;
    serverRevision?: number;
    archivedAt?: string;
  };
  createdAt: string;
  updatedAt: string;
}

export interface GameExportConfig {
  gameTitle: string;
  outputDirectory: string;
  locale?: string;
  entryNodeId?: string;
  windowMode: 'windowed' | 'fullscreen' | 'borderless';
  resolution: {
    width: number;
    height: number;
  };
  includeDebugOverlay: boolean;
}

export type SceneTurnExecutionMode = 'cli';

export type SceneTurnAgentId = 'codex' | 'claude' | 'gemini' | 'kimi' | 'qwen' | 'opencode';

export type SceneTurnByokProviderId = 'anthropic' | 'openai-compatible' | 'google-gemini' | 'ollama';

export type SceneTurnMediaProviderId = 'openai-image' | 'doubao-image' | 'doubao-video' | 'google-imagen' | 'google-veo' | 'kling-video' | 'minimax-video' | 'minimax-tts' | 'elevenlabs-audio' | 'senseaudio';

export type SceneTurnMediaProviderType = 'image' | 'video' | 'audio';

export interface SceneTurnAgentInfo {
  id: SceneTurnAgentId;
  name: string;
  bin: string;
  version: string;
  available: boolean;
  models: string[];
  reasoningOptions?: string[];
}

export interface SceneTurnCliSelection {
  agentId: SceneTurnAgentId;
  model: string;
  reasoningEffort?: string;
}

export interface SceneTurnByokProviderConfig {
  providerId: SceneTurnByokProviderId;
  apiKey: string;
  baseUrl: string;
  model: string;
}

export interface SceneTurnMediaProviderConfig {
  providerId: SceneTurnMediaProviderId;
  apiKey: string;
  baseUrl: string;
  model: string;
}

export interface SceneTurnAiConfig {
  executionMode: SceneTurnExecutionMode;
  selectedCliAgentId: SceneTurnAgentId;
  cliSelections: SceneTurnCliSelection[];
  selectedByokProviderId: SceneTurnByokProviderId;
  byokProviders: SceneTurnByokProviderConfig[];
  mediaProviders: SceneTurnMediaProviderConfig[];
}

export interface SceneTurnConnectionTestResult {
  ok: boolean;
  message: string;
}

export interface SceneTurnChatAttachment {
  name: string;
  type: string;
  size: number;
  content?: string;
  truncated?: boolean;
}

export interface SceneTurnChatMessage {
  role: 'user' | 'assistant';
  content: string;
  attachments?: SceneTurnChatAttachment[];
}

export interface SceneTurnChatRequest {
  messages: SceneTurnChatMessage[];
}

export interface SceneTurnChatResponse {
  ok: boolean;
  content: string;
  agentId: SceneTurnAgentId;
  model: string;
  error?: string;
}

export type AppNode = Node<NodeData, NodeType>;

export interface NodeTimeline {
  version: 2;
  duration: number;
  tracks: TimelineTrack[];
  bookmarks: TimelineBookmark[];
  playheadTime?: number;
  zoom?: number;
}

export type TimelineTrackType = 'media' | 'interaction';

export interface TimelineBookmark {
  id: string;
  time: number;
  label?: string;
  color?: string;
}

export interface TimelineTrack {
  id: string;
  type: TimelineTrackType;
  name: string;
  mediaRole?: 'main' | 'overlay' | 'audio';
  locked?: boolean;
  hidden?: boolean;
  muted?: boolean;
  collapsed?: boolean;
  clips: TimelineClip[];
}

export type TimelineMediaClipType = 'video' | 'image' | 'audio';
export type TimelineInteractionClipType = 'button';
export type TimelineClipType = TimelineMediaClipType | TimelineInteractionClipType;
export type TimelineKeyframeProperty = 'opacity' | 'rotation' | 'x' | 'y' | 'width' | 'height' | 'volume';
export type TimelineKeyframeInterpolation = 'linear' | 'hold';
export type ButtonMode = 'normal' | 'qte';
export type ButtonQteInput = 'click' | 'space';
export type ButtonStylePreset = 'solid' | 'outline' | 'glass' | 'ghost';
export type ButtonStyleShape = 'rounded' | 'pill' | 'square' | 'oval' | 'diamond' | 'hexagon';
export type ButtonStyleShadow = 'none' | 'soft' | 'strong';
export type ButtonStyleBackgroundFit = 'cover' | 'contain' | 'stretch';

export interface TimelineClipKeyframe {
  id: string;
  property: TimelineKeyframeProperty;
  time: number;
  value: number;
  interpolation?: TimelineKeyframeInterpolation;
}

export type TimelineClip = TimelineMediaClip | TimelineInteractionClip;

export interface BaseTimelineClip {
  id: string;
  type: TimelineClipType;
  startTime: number;
  duration: number;
  sourceStart?: number;
  sourceDuration?: number;
  name?: string;
  enabled: boolean;
  hidden?: boolean;
  opacity?: number;
  rotation?: number;
  linkGroupId?: string;
  keyframes?: TimelineClipKeyframe[];
}

export interface OverlayRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface TimelineMediaClip extends BaseTimelineClip {
  type: TimelineMediaClipType;
  src: string;
  assetId?: string;
  poster?: string;
  playbackId?: string;
  muted?: boolean;
  volume?: number;
  playbackRate?: number;
  preservePitch?: boolean;
  freezeFrameTime?: number;
  fit?: 'contain' | 'cover';
  rect?: OverlayRect;
  sourceAudioEnabled?: boolean;
  sourceVideoClipId?: string;
}

export interface ButtonQteConfig {
  input: ButtonQteInput;
  prompt?: string;
  clickCount?: number;
  keyLabel?: string;
  showCountdown?: boolean;
  showCueLabel?: boolean;
}

export interface ButtonStyleConfig {
  preset?: ButtonStylePreset;
  shape?: ButtonStyleShape;
  fillColor?: string;
  textColor?: string;
  borderColor?: string;
  fillOpacity?: number;
  borderOpacity?: number;
  borderWidth?: number;
  shadow?: ButtonStyleShadow;
  backgroundImageAssetId?: string | undefined;
  backgroundImageSrc?: string | undefined;
  backgroundImageFit?: ButtonStyleBackgroundFit | undefined;
}

export interface ButtonChoiceClip extends BaseTimelineClip {
  type: 'button';
  mode?: ButtonMode;
  label: string;
  rect: OverlayRect;
  pauseOnShow: boolean;
  qte?: ButtonQteConfig;
  style?: ButtonStyleConfig;
}

export type TimelineInteractionClip = ButtonChoiceClip;

export type NodeData =
  | { type: 'start'; label?: string; title?: string; bodyText?: string; timeline?: NodeTimeline }
  | { type: 'scene'; title: string; bodyText?: string; timeline?: NodeTimeline }
  | { type: 'end'; label?: string; title?: string; bodyText?: string; timeline?: NodeTimeline };

export type AppEdge = Edge;
