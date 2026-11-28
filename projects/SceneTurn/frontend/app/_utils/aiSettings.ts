import {
  SceneTurnAgentId,
  SceneTurnAgentInfo,
  SceneTurnAiConfig,
  SceneTurnByokProviderConfig,
  SceneTurnByokProviderId,
  SceneTurnChatRequest,
  SceneTurnChatResponse,
  SceneTurnConnectionTestResult,
  SceneTurnMediaProviderConfig,
  SceneTurnMediaProviderId,
  SceneTurnMediaProviderType,
} from '../_types';

import aiDefinitions from '../../shared/ai-definitions';
import { AI_SETTINGS_STORAGE_KEY, readSceneTurnStorage, writeSceneTurnStorage } from './browserStorage';

export const AI_SETTINGS_CHANGE_EVENT = 'sceneturn-ai-settings-changed';

export interface SceneTurnAgentDefinition {
  id: SceneTurnAgentId;
  name: string;
  bin: string;
  models: string[];
  reasoningOptions?: string[];
}

export interface SceneTurnByokProviderDefinition {
  id: SceneTurnByokProviderId;
  name: string;
  defaultBaseUrl: string;
  defaultModel: string;
}

export interface SceneTurnMediaProviderDefinition {
  id: SceneTurnMediaProviderId;
  name: string;
  types: SceneTurnMediaProviderType[];
  defaultBaseUrl: string;
  defaultModel: string;
}

export const sceneTurnAgentDefinitions = aiDefinitions.agentDefinitions as SceneTurnAgentDefinition[];
export const sceneTurnByokProviderDefinitions = aiDefinitions.byokProviderDefinitions.map(({ needsKey, ...provider }) => provider) as SceneTurnByokProviderDefinition[];
export const sceneTurnMediaProviderDefinitions = aiDefinitions.mediaProviderDefinitions as SceneTurnMediaProviderDefinition[];
export const getDefaultSceneTurnAiConfig = (): SceneTurnAiConfig => aiDefinitions.getDefaultAiConfig() as SceneTurnAiConfig;
export const normalizeSceneTurnAiConfig = (value: unknown): SceneTurnAiConfig => aiDefinitions.normalizeAiConfig(value) as SceneTurnAiConfig;

export const getSceneTurnAiConfig = async (): Promise<SceneTurnAiConfig> => {
  if (typeof window === 'undefined') return getDefaultSceneTurnAiConfig();
  if (window.sceneturn?.getAiConfig) return normalizeSceneTurnAiConfig(await window.sceneturn.getAiConfig());
  if (!window.sceneturn) return getDefaultSceneTurnAiConfig();
  const raw = readSceneTurnStorage(window.localStorage, AI_SETTINGS_STORAGE_KEY);
  if (!raw) return getDefaultSceneTurnAiConfig();
  try {
    return normalizeSceneTurnAiConfig(JSON.parse(raw));
  } catch {
    return getDefaultSceneTurnAiConfig();
  }
};

export const saveSceneTurnAiConfig = async (config: SceneTurnAiConfig): Promise<SceneTurnAiConfig> => {
  const normalized = normalizeSceneTurnAiConfig(config);
  if (typeof window === 'undefined') return normalized;
  if (!window.sceneturn) throw new Error('请通过受保护的生成设置保存服务凭据。');
  const saved = window.sceneturn.saveAiConfig
    ? normalizeSceneTurnAiConfig(await window.sceneturn.saveAiConfig(normalized))
    : normalized;
  if (!window.sceneturn.saveAiConfig) {
    writeSceneTurnStorage(window.localStorage, AI_SETTINGS_STORAGE_KEY, JSON.stringify(saved));
  }
  window.dispatchEvent(new CustomEvent(AI_SETTINGS_CHANGE_EVENT, { detail: saved }));
  return saved;
};

export const detectSceneTurnAiAgents = async (): Promise<SceneTurnAgentInfo[]> => {
  if (typeof window !== 'undefined' && window.sceneturn?.detectAiAgents) {
    return window.sceneturn.detectAiAgents();
  }
  if (typeof window !== 'undefined' && !window.sceneturn) {
    return sceneTurnAgentDefinitions.map((agent) => ({ ...agent, version: '', available: false }));
  }
  if (typeof window !== 'undefined') {
    try {
      const response = await fetch('/api/ai-settings/agents', { cache: 'no-store' });
      if (response.ok) {
        const payload = await response.json() as { agents?: SceneTurnAgentInfo[] };
        if (Array.isArray(payload.agents)) return payload.agents;
      }
    } catch {
    }
  }
  return sceneTurnAgentDefinitions.map((agent) => ({
    id: agent.id,
    name: agent.name,
    bin: agent.bin,
    version: '',
    available: false,
    models: agent.models,
    reasoningOptions: agent.reasoningOptions,
  }));
};

export const testSceneTurnAiAgent = async (agentId: SceneTurnAgentId): Promise<SceneTurnConnectionTestResult> => {
  if (typeof window !== 'undefined' && window.sceneturn?.testAiAgent) return window.sceneturn.testAiAgent(agentId);
  if (typeof window !== 'undefined' && !window.sceneturn) return { ok: false, message: '尚未连接本人的本机Codex。' };
  if (typeof window !== 'undefined') {
    try {
      const response = await fetch(`/api/ai-settings/agents/${agentId}/test`, { method: 'POST', cache: 'no-store' });
      if (response.ok) return await response.json() as SceneTurnConnectionTestResult;
    } catch {
    }
  }
  return { ok: false, message: 'Current browser environment cannot test local CLI.' };
};

export const sendSceneTurnChatMessage = async (request: SceneTurnChatRequest): Promise<SceneTurnChatResponse> => {
  const config = await getSceneTurnAiConfig();
  const selection = config.cliSelections.find((item) => item.agentId === config.selectedCliAgentId);
  const model = selection?.model || sceneTurnAgentDefinitions.find((item) => item.id === config.selectedCliAgentId)?.models[0] || '';

  if (typeof window !== 'undefined' && window.sceneturn?.sendChatMessage) {
    return window.sceneturn.sendChatMessage(request);
  }

  return {
    ok: false,
    content: '',
    agentId: config.selectedCliAgentId,
    model,
    error: '尚未连接本人的本机Codex。视频生成请使用剧情素材画布。',
  };
};

export const testSceneTurnByokProvider = async (provider: SceneTurnByokProviderConfig): Promise<SceneTurnConnectionTestResult> => {
  if (typeof window !== 'undefined' && window.sceneturn?.testByokProvider) return window.sceneturn.testByokProvider(provider);
  if (typeof window !== 'undefined' && !window.sceneturn) return { ok: false, message: '云端工作台通过服务端配置管理模型服务。' };
  return { ok: Boolean(provider.model && (provider.providerId === 'ollama' || provider.apiKey)), message: provider.model ? 'Configuration looks valid' : 'Enter a model name' };
};

export const testSceneTurnMediaProvider = async (provider: SceneTurnMediaProviderConfig): Promise<SceneTurnConnectionTestResult> => {
  if (typeof window !== 'undefined' && window.sceneturn?.testMediaProvider) return window.sceneturn.testMediaProvider(provider);
  if (typeof window !== 'undefined' && !window.sceneturn) return { ok: false, message: '请通过生成设置保存配置，再从剧情画布提交真实任务。' };
  return { ok: Boolean(provider.apiKey && provider.model), message: provider.apiKey && provider.model ? 'Configuration looks valid' : 'Enter API key and model' };
};
