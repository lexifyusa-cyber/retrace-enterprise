const { app, BrowserWindow, shell } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const http = require('http')
const os = require('os')
const fs = require('fs')

const isDev = process.env.NODE_ENV === 'development' || !app.isPackaged

let mainWindow
let backendProcess = null

const BACKEND_PORT = 8000
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`

function getAppSupportDir() {
  const dir = process.platform === 'win32'
    ? path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'KnowledgePod')
    : path.join(os.homedir(), 'Library', 'Application Support', 'KnowledgePod')
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })
  return dir
}

function getBackendPath() {
  if (isDev) {
    return path.join(__dirname, '..', '..', 'backend')
  }
  return path.join(process.resourcesPath, 'backend')
}

function getStaticDir() {
  const backendDir = getBackendPath()
  const bundled = path.join(backendDir, 'frontend-dist')
  if (fs.existsSync(bundled)) return bundled
  return ''
}

function getBackendEnv() {
  const appSupport = getAppSupportDir()
  const dbPath = path.join(appSupport, 'knowledgepod.db')
  const staticDir = getStaticDir()
  return {
    ...process.env,
    DATABASE_URL: `sqlite+aiosqlite:///${dbPath}`,
    APP_ENV: 'production',
    DEBUG: 'false',
    HOST: '127.0.0.1',
    PORT: String(BACKEND_PORT),
    CHROMADB_PATH: path.join(appSupport, 'chromadb'),
    ...(staticDir ? { STATIC_DIR: staticDir } : {}),
  }
}

function startBackend() {
  const backendDir = getBackendPath()
  const venvPython = process.platform === 'win32'
    ? path.join(backendDir, 'venv', 'Scripts', 'python.exe')
    : path.join(backendDir, 'venv', 'bin', 'python')

  const pythonBin = fs.existsSync(venvPython) ? venvPython : (process.platform === 'win32' ? 'python' : 'python3')
  const env = getBackendEnv()

  console.log(`[Electron] Starting backend from ${backendDir}`)
  console.log(`[Electron] Python: ${pythonBin}`)
  console.log(`[Electron] DB: ${env.DATABASE_URL}`)

  backendProcess = spawn(pythonBin, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT), '--log-level', 'warning'], {
    cwd: backendDir,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  backendProcess.stdout.on('data', (data) => {
    console.log(`[Backend] ${data.toString().trim()}`)
  })
  backendProcess.stderr.on('data', (data) => {
    console.error(`[Backend] ${data.toString().trim()}`)
  })
  backendProcess.on('error', (err) => {
    console.error(`[Electron] Failed to start backend: ${err.message}`)
  })
  backendProcess.on('exit', (code) => {
    console.log(`[Electron] Backend exited with code ${code}`)
    backendProcess = null
  })
}

function stopBackend() {
  if (backendProcess) {
    console.log('[Electron] Stopping backend...')
    backendProcess.kill('SIGTERM')
    setTimeout(() => {
      if (backendProcess && !backendProcess.killed) {
        backendProcess.kill('SIGKILL')
      }
    }, 3000)
    backendProcess = null
  }
}

function waitForBackend(timeout = 30000) {
  const start = Date.now()
  return new Promise((resolve, reject) => {
    const check = () => {
      if (Date.now() - start > timeout) {
        return reject(new Error('Backend did not start in time'))
      }
      const req = http.get(`${BACKEND_URL}/`, (res) => {
        if (res.statusCode === 200) return resolve()
        setTimeout(check, 300)
      })
      req.on('error', () => setTimeout(check, 300))
      req.end()
    }
    check()
  })
}

function getLoadingHTML() {
  return `
    <!DOCTYPE html>
    <html>
    <head>
      <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
          background: #1e2128;
          color: #eceff4;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          display: flex;
          align-items: center;
          justify-content: center;
          height: 100vh;
          -webkit-app-region: no-drag;
        }
        .container { text-align: center; }
        .logo { font-size: 48px; margin-bottom: 24px; }
        h1 { font-size: 24px; font-weight: 600; margin-bottom: 8px; }
        p { font-size: 14px; color: #9ca3af; margin-bottom: 32px; }
        .spinner {
          width: 32px; height: 32px;
          border: 3px solid #3b4252;
          border-top: 3px solid #88c0d0;
          border-radius: 50%;
          animation: spin 1s linear infinite;
          margin: 0 auto;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
      </style>
    </head>
    <body>
      <div class="container">
        <div class="logo">🧠</div>
        <h1>KnowledgePod</h1>
        <p>Starting up...</p>
        <div class="spinner"></div>
      </div>
    </body>
    </html>
  `
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    titleBarStyle: 'default',
    backgroundColor: '#1e2128',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  })

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.show()
    mainWindow.webContents.openDevTools()
    return
  }

  // Production: show loading screen, start backend, then load app
  mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(getLoadingHTML())}`)
  mainWindow.show()

  startBackend()

  try {
    await waitForBackend()
    console.log('[Electron] Backend is ready, loading app')
    mainWindow.loadURL(BACKEND_URL)
  } catch (err) {
    console.error('[Electron]', err.message)
    mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`
      <!DOCTYPE html><html><head><style>
        body { background:#1e2128; color:#eceff4; font-family:sans-serif;
               display:flex; align-items:center; justify-content:center; height:100vh; }
        .err { text-align:center; }
        h1 { color:#bf616a; margin-bottom:12px; }
        p { color:#9ca3af; }
      </style></head><body><div class="err">
        <h1>Failed to start</h1>
        <p>${err.message}</p>
        <p style="margin-top:16px;font-size:12px">Check Console.app for details or restart the application.</p>
      </div></body></html>
    `)}`)
  }
}

app.whenReady().then(createWindow)

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    stopBackend()
    app.quit()
  }
})

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow()
  }
})

app.on('before-quit', () => {
  stopBackend()
})
