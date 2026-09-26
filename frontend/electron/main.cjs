// Electron 主进程：启动本地 Python 后端 + 加载前端
const { app, BrowserWindow, shell } = require('electron')
const { spawn } = require('child_process')
const path = require('path')
const fs = require('fs')
const os = require('os')
const { waitForBackend } = require('./wait-for-backend.cjs')
const { stopBackendProcess } = require('./stop-backend.cjs')

const BACKEND_PORT = 8000
let backendProcess = null

// 后端日志留一份文件。后端进程的 stdout/stderr 被 startBackend 用管道接走了
// （stdio: 'pipe'），所以 run_desktop.py 里那个「stdout 是 None 才写 backend.log」的兜底
// **不会触发** —— 而打包态的 Electron 主进程没有控制台，console.log 出去就没了。
// 结果就是：后端起不来时用户只看到「窗口打不开」，哪儿都没有线索（本轮排查时正撞上
// 这一点）。所以在这里再抄一份到文件，与 config.json / 数据库同一个目录，用户可以直接
// 把它发回来。
const BACKEND_LOG = path.join(os.homedir(), '.academic_writer', 'backend.log')
let logChecked = false

function appendBackendLog(line) {
  try {
    fs.mkdirSync(path.dirname(BACKEND_LOG), { recursive: true })
    if (!logChecked) {
      logChecked = true
      // 跨启动只做一次：uvicorn 的 access log **每来一个请求就一行**，不封顶会一直长
      try {
        if (fs.statSync(BACKEND_LOG).size > 2 * 1024 * 1024) fs.rmSync(BACKEND_LOG)
      } catch (e) {}
    }
    fs.appendFileSync(BACKEND_LOG, line)
  } catch (e) {}
}

// 定位后端启动脚本
function getBackendCommand() {
  const isDev = !app.isPackaged
  if (isDev) {
    // 开发模式：直接用系统 python 运行 backend 源码
    const backendDir = path.join(__dirname, '..', '..', 'backend')
    return { cmd: 'python', args: ['-m', 'uvicorn', 'app.main:app', '--port', String(BACKEND_PORT)], cwd: backendDir }
  }
  // 打包模式：使用捆绑的 PyInstaller 后端可执行文件
  const resources = process.resourcesPath
  const backendExe = path.join(resources, 'backend', 'academic_backend.exe')
  if (fs.existsSync(backendExe)) {
    return {
      cmd: backendExe,
      args: [],
      cwd: path.join(resources, 'backend'),
    }
  }
  // 兜底：尝试系统 python 运行源码
  const backendDir = path.join(resources, 'backend')
  return { cmd: 'python', args: ['-m', 'uvicorn', 'app.main:app', '--port', String(BACKEND_PORT)], cwd: backendDir }
}

function startBackend() {
  const { cmd, args, cwd } = getBackendCommand()
  console.log(`[Electron] 启动后端: ${cmd} ${args.join(' ')} (cwd=${cwd})`)
  try {
    backendProcess = spawn(cmd, args, {
      cwd,
      stdio: 'pipe',
      windowsHide: true,
    })
    backendProcess.stdout.on('data', (d) => {
      console.log(`[backend] ${d}`)
      appendBackendLog(`[out] ${d}`)
    })
    backendProcess.stderr.on('data', (d) => {
      console.log(`[backend:err] ${d}`)
      appendBackendLog(`[err] ${d}`)
    })
    backendProcess.on('exit', (code) => {
      console.log(`[Electron] 后端退出，code=${code}`)
      appendBackendLog(`[exit] 后端退出 code=${code}\n`)
      backendProcess = null
    })
  } catch (e) {
    console.error('[Electron] 后端启动失败:', e)
  }
}

function stopBackend() {
  // 先取出引用再置空：这个函数会被 window-all-closed 与 will-quit 各调一次，
  // 第二次必须是安全的 no-op。杀法（含 Windows 上为什么要 taskkill /T）见
  // stop-backend.cjs —— 那是个不含 electron 的纯模块，所以有 node:test 覆盖。
  const child = backendProcess
  backendProcess = null
  try {
    stopBackendProcess(child)
  } catch (e) {}
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    title: 'AI 学术写作辅助系统',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  const isDev = !app.isPackaged
  if (isDev) {
    win.loadURL('http://localhost:5173')  // Vite dev server
  } else {
    win.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  }

  // 外部链接用系统浏览器打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })
}

app.whenReady().then(async () => {
  startBackend()
  const ready = await waitForBackend({ port: BACKEND_PORT })
  if (!ready) {
    // 这条必须落文件：后端起不来时窗口会照开（只是所有接口都失败），
    // 而主进程的 console.error 在打包态是没人看得见的。
    const msg = '[Electron] 后端启动超时（30 秒内 /api/health 没通过）\n'
    console.error(msg)
    appendBackendLog(msg)
  }
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  stopBackend()
  if (process.platform !== 'darwin') app.quit()
})

app.on('will-quit', () => {
  stopBackend()
})
