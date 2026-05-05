const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, shell } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const fs = require('fs')

let mainWindow = null
let tray = null
let backendProcess = null

// ─── Backend ───────────────────────────────────────────────────────────────────
function startBackend() {
  const backendDir = path.join(__dirname, '..', 'backend')
  const venvPython = path.join(backendDir, 'venv', 'bin', 'python')
  const python = fs.existsSync(venvPython) ? venvPython : 'python3'

  // Load .env values to pass through
  const envFile = path.join(backendDir, '.env')
  const envVars = {}
  if (fs.existsSync(envFile)) {
    fs.readFileSync(envFile, 'utf8').split('\n').forEach(line => {
      const m = line.match(/^([A-Z_]+)=(.*)$/)
      if (m) envVars[m[1]] = m[2].trim()
    })
  }

  backendProcess = spawn(python, ['bot.py'], {
    cwd: backendDir,
    env: { ...process.env, ...envVars },
    windowsHide: true,
  })

  backendProcess.stdout.on('data', d => console.log('[backend]', d.toString().trim()))
  backendProcess.stderr.on('data', d => console.error('[backend]', d.toString().trim()))
  backendProcess.on('close', code => console.log('[backend] exited:', code))
}

// ─── Window ────────────────────────────────────────────────────────────────────
function createWindow() {
  const iconPath = path.join(__dirname, '..', 'assets', 'icon.icns')
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 780,
    minWidth: 900,
    minHeight: 620,
    titleBarStyle: 'hiddenInset',
    vibrancy: 'under-window',
    visualEffectState: 'active',
    backgroundColor: '#08090b',
    trafficLightPosition: { x: 16, y: 15 },
    icon: fs.existsSync(iconPath) ? iconPath : undefined,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  })

  mainWindow.loadFile(path.join(__dirname, '..', 'frontend', 'index.html'))
  mainWindow.on('closed', () => { mainWindow = null })
}

// ─── Tray ──────────────────────────────────────────────────────────────────────
function createTray() {
  const iconPath = path.join(__dirname, 'tray-icon.png')
  let icon
  try { icon = nativeImage.createFromPath(iconPath) }
  catch { icon = nativeImage.createEmpty() }

  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon.resize({ width: 16, height: 16 }))
  const menu = Menu.buildFromTemplate([
    { label: 'Show TradeBot', click: () => mainWindow ? mainWindow.show() : createWindow() },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ])
  tray.setToolTip('TradeBot')
  tray.setContextMenu(menu)
  tray.on('double-click', () => mainWindow ? mainWindow.show() : createWindow())
}

// ─── IPC ───────────────────────────────────────────────────────────────────────
ipcMain.handle('open-external', (_, url) => shell.openExternal(url))

ipcMain.handle('set-login-item', (_, enable) => {
  app.setLoginItemSettings({ openAtLogin: enable, openAsHidden: false })
  return enable
})

ipcMain.handle('get-login-item', () => {
  return app.getLoginItemSettings().openAtLogin
})

// ─── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  app.setName('TradeBot')

  // Sync open-at-login from settings.json on every launch
  try {
    const settingsPath = path.join(__dirname, '..', 'backend', 'settings.json')
    if (fs.existsSync(settingsPath)) {
      const s = JSON.parse(fs.readFileSync(settingsPath, 'utf8'))
      app.setLoginItemSettings({ openAtLogin: !!s.open_at_login, openAsHidden: false })
    }
  } catch {}

  startBackend()
  setTimeout(() => {
    createWindow()
    createTray()
  }, 1800)
})

app.on('window-all-closed', () => {
  // On macOS keep running in tray
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (mainWindow === null) createWindow()
})

app.on('before-quit', () => {
  if (backendProcess) backendProcess.kill()
})
