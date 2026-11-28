export const AI_SETTINGS_STORAGE_KEY = 'sceneturn-ai-settings';
export const LOCAL_PROJECTS_STORAGE_KEY = 'sceneturn-local-projects';
export const EDITOR_STORAGE_KEY = 'sceneturn-editor-storage';
export const LOCALE_STORAGE_KEY = 'sceneturn.locale';

const legacyStorageKeys = new Map([
  [AI_SETTINGS_STORAGE_KEY, 'openfmv-ai-settings'],
  [LOCAL_PROJECTS_STORAGE_KEY, 'openfmv-local-projects'],
  [EDITOR_STORAGE_KEY, 'openfmv-editor-storage'],
]);

export function readSceneTurnStorage(storage: Storage, key: string) {
  const current = storage.getItem(key);
  const previousKey = legacyStorageKeys.get(key);
  return current ?? (previousKey ? storage.getItem(previousKey) : null);
}

export function writeSceneTurnStorage(storage: Storage, key: string, value: string) {
  storage.setItem(key, value);
  const previousKey = legacyStorageKeys.get(key);
  if (previousKey) storage.removeItem(previousKey);
}

export function removeSceneTurnStorage(storage: Storage, key: string) {
  storage.removeItem(key);
  const previousKey = legacyStorageKeys.get(key);
  if (previousKey) storage.removeItem(previousKey);
}
