const { contextBridge, ipcRenderer } = require('electron');
const { createSceneTurnBridge } = require('../shared/ipc-contract.js');

contextBridge.exposeInMainWorld(
  'sceneturn',
  createSceneTurnBridge((channel, ...args) => ipcRenderer.invoke(channel, ...args))
);
