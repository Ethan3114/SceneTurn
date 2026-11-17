import { create } from 'zustand';
import { createMaterialConversation, getMaterialConversation, listMaterialConversations, type MaterialConversation, type MaterialReference } from '@/app/_utils/materialApi';

type Scope = { projectId: string; sceneId: string };
type State = {
  scope: Scope | null; chatOpen: boolean; conversation: MaterialConversation | null;
  references: MaterialReference[]; referenceAssetIds: string[]; draft: string; error: string; loading: boolean;
  open: (scope: Scope) => Promise<void>; close: () => void; refresh: () => Promise<void>;
  select: (reference: MaterialReference) => void; remove: (id: string) => void;
  setConversation: (value: MaterialConversation) => void;
  savePending: () => Promise<void>;
};

export const useMaterialWorkspace = create<State>((set, get) => ({
  scope: null, chatOpen: false, conversation: null, references: [], referenceAssetIds: [], draft: '', error: '', loading: false,
  savePending: async () => {},
  open: async (scope) => {
    if (get().scope?.projectId === scope.projectId && get().scope?.sceneId === scope.sceneId) { set({ chatOpen: true }); return; }
    set({ scope, chatOpen: true, loading: true, error: '', conversation: null, references: [], referenceAssetIds: [], draft: '' });
    try {
      const items = await listMaterialConversations(scope.projectId, scope.sceneId);
      if (get().scope !== scope) return;
      const value = items.length ? await getMaterialConversation(scope.projectId, scope.sceneId, items[0].id) : await createMaterialConversation(scope.projectId, scope.sceneId);
      if (get().scope === scope) set({ conversation: value, loading: false });
    } catch (error) { if (get().scope === scope) set({ error: error instanceof Error ? error.message : '素材对话读取失败', loading: false }); }
  },
  close: () => set({ scope: null, chatOpen: false, conversation: null, references: [], referenceAssetIds: [], draft: '' }),
  refresh: async () => {
    const value = get().conversation;
    if (!value) return;
    const updated = await getMaterialConversation(value.projectId, value.sceneId, value.id);
    if (get().conversation?.id === value.id) set({ conversation: updated, error: '' });
  },
  select: (reference) => set((state) => ({ chatOpen: true, references: [...state.references.filter((item) => item.nodeId !== reference.nodeId), reference].slice(-20) })),
  remove: (id) => set((state) => ({ references: state.references.filter((item) => item.nodeId !== id) })),
  setConversation: (conversation) => set({ conversation, error: '' }),
}));

export const materialRoles = [
  { id: 'director', title: '总监', description: '制作简报与岗位分工', color: '#f97316' },
  { id: 'writer', title: '编剧', description: '剧本、对白与故事素材', color: '#eab308' },
  { id: 'character_designer', title: '角色设计师', description: '角色设定与角色图片', color: '#a78bfa' },
  { id: 'scene_designer', title: '场景设计师', description: '场景、道具与空间设计', color: '#34d399' },
  { id: 'storyboarder', title: '分镜师', description: '镜头设计与分镜图片', color: '#60a5fa' },
  { id: 'media_producer', title: '视频师', description: '镜头视频与生成结果', color: '#f472b6' },
  { id: 'sound_designer', title: '声音设计师', description: '配音与声音设计', color: '#2dd4bf' },
];
