import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

import { EDITOR_STORAGE_KEY, readSceneTurnStorage, removeSceneTurnStorage, writeSceneTurnStorage } from '@/app/_utils/browserStorage';

export type EdgeCurveStyle = 'smoothstep' | 'bezier' | 'straight';

const DEFAULT_AUTO_SAVE_ENABLED = true;

const isQuotaExceededError = (error: unknown) => {
  return error instanceof DOMException && (
    error.name === 'QuotaExceededError' ||
    error.name === 'NS_ERROR_DOM_QUOTA_REACHED' ||
    error.code === 22 ||
    error.code === 1014
  );
};

const createSafeLocalStorage = () => ({
  getItem: (name: string) => readSceneTurnStorage(localStorage, name),
  setItem: (name: string, value: string) => {
    try {
      writeSceneTurnStorage(localStorage, name, value);
    } catch (error) {
      if (!isQuotaExceededError(error)) throw error;
      localStorage.removeItem(name);
      try {
        writeSceneTurnStorage(localStorage, name, value);
      } catch (retryError) {
        if (!isQuotaExceededError(retryError)) throw retryError;
      }
    }
  },
  removeItem: (name: string) => removeSceneTurnStorage(localStorage, name),
});

interface EditorState {
  selectedNodeId: string | null;
  setSelectedNodeId: (id: string | null) => void;
  autoSaveEnabled: boolean;
  setAutoSaveEnabled: (enabled: boolean) => void;
  isAssetPickerOpen: boolean;
  setAssetPickerOpen: (isOpen: boolean) => void;
  assetWorkspaceView: 'library' | 'generate';
  setAssetWorkspaceView: (view: EditorState['assetWorkspaceView']) => void;
  targetNodeIdForAsset: string | null;
  setTargetNodeIdForAsset: (id: string | null) => void;
  edgeCurveStyle: EdgeCurveStyle;
  setEdgeCurveStyle: (style: EdgeCurveStyle) => void;
  reset: () => void;
}

export const useEditorStore = create<EditorState>()(
  persist(
    (set) => ({
      selectedNodeId: null,
      autoSaveEnabled: DEFAULT_AUTO_SAVE_ENABLED,
      isAssetPickerOpen: false,
      assetWorkspaceView: 'library',
      targetNodeIdForAsset: null,
      edgeCurveStyle: 'bezier',

      setSelectedNodeId: (id) => set({ selectedNodeId: id }),
      setAutoSaveEnabled: (enabled) => set({ autoSaveEnabled: enabled }),
      setAssetPickerOpen: (isOpen) => set(isOpen ? { isAssetPickerOpen: true } : { isAssetPickerOpen: false, assetWorkspaceView: 'library' }),
      setAssetWorkspaceView: (assetWorkspaceView) => set({ assetWorkspaceView }),
      setTargetNodeIdForAsset: (id) => set({ targetNodeIdForAsset: id }),
      setEdgeCurveStyle: (style) => set({ edgeCurveStyle: style }),
      reset: () => set({
        selectedNodeId: null,
        isAssetPickerOpen: false,
        assetWorkspaceView: 'library',
        targetNodeIdForAsset: null,
      }),
    }),
    {
      name: EDITOR_STORAGE_KEY,
      version: 3,
      storage: createJSONStorage(createSafeLocalStorage),
      partialize: (state) => ({
        autoSaveEnabled: state.autoSaveEnabled,
        edgeCurveStyle: state.edgeCurveStyle,
      }),
      migrate: (persistedState) => {
        const state = persistedState as Partial<EditorState>;
        return {
          autoSaveEnabled: state.autoSaveEnabled ?? DEFAULT_AUTO_SAVE_ENABLED,
          edgeCurveStyle: state.edgeCurveStyle ?? 'bezier',
        };
      },
      merge: (persistedState, currentState) => ({
        ...currentState,
        ...(persistedState as Partial<EditorState>),
        selectedNodeId: null,
        isAssetPickerOpen: false,
        assetWorkspaceView: 'library',
        targetNodeIdForAsset: null,
      }),
    }
  )
);

export const isAssetWorkspaceOpen = (state: EditorState) => state.isAssetPickerOpen && !state.targetNodeIdForAsset;
