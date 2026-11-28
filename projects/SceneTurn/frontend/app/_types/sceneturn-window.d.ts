import type { SceneTurnBridge } from '../../shared/ipc-contract';

declare global {
  interface Window {
    sceneturn?: SceneTurnBridge;
  }
}

export {};
