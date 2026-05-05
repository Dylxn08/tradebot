const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electron', {
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  setLoginItem: (enable) => ipcRenderer.invoke('set-login-item', enable),
  getLoginItem: () => ipcRenderer.invoke('get-login-item'),
})
