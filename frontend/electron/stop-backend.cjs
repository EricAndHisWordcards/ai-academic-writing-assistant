// 关闭应用时收掉后端进程。
//
// 从 main.cjs 里抽出来，纯粹为了能测（同 wait-for-backend.cjs 的理由：main.cjs 顶层
// `require('electron')` 在普通 node 下 import 不进来）。spawn、tasklist 的读取、pid
// 文件的路径都做成可注入，测试里塞假的就能断言各条分支，不引真进程。
'use strict'

const fs = require('fs')
const os = require('os')
const path = require('path')

// 后端在启动时写的「真正跑 Python 的那个 pid」的落点（见 backend/run_desktop.py 的
// _write_backend_pid）。目录与 main.cjs 的 backend.log 同一个。
const PID_FILE = path.join(os.homedir(), '.academic_writer', 'backend.pid')

// PyInstaller 打出来的可执行文件名（run_desktop.spec 里定的）。核对它**排除不了父子**
// —— 两层进程同名 —— 它排除的是「进程号被别人复用了」：pid 会被回收，一个几天前写下的
// 号码完全可能落到一个毫不相干的程序上，那时候照 pid 杀就是误杀别人。核对不过就回落。
const BACKEND_IMAGE = 'academic_backend.exe'


// 读 pid 文件。**每一种读不出来的方式都返回 null，一律走回落**：文件不存在（后端没起来
// 过、或被上一次关闭删掉了）、读不出、内容是空的或不是正整数。不抛异常 —— 关闭路径上
// 抛出去会让后面的回落也不执行，那就真的关不掉了。
function readPidFile(pidFile) {
  try {
    const pid = Number(fs.readFileSync(pidFile, 'utf8').trim())
    return Number.isInteger(pid) && pid > 0 ? pid : null
  } catch (e) {
    return null
  }
}


// 用 tasklist 核对「这个 pid 现在真的是那个镜像」。
//
// 判据是**从 CSV 的两个字段里比出来的**，不是「输出非空」：tasklist 没命中时也会往
// stdout 写一句提示语（中文系统上是「信息: 没有运行的任务匹配指定标准。」），
// 非空判定会把每一次核对都放行 —— 那等于没有核对。
//   /FO CSV /NH 命中时长这样：`"academic_backend.exe","4242","Console","1","12,345 K"`
function isBackendAlive(pid, execFn, image) {
  try {
    const out = execFn('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], {
      windowsHide: true, encoding: 'utf8',
    })
    const m = String(out).match(/"([^"]+)","(\d+)"/)
    return !!m && Number(m[2]) === pid && m[1].toLowerCase() === image.toLowerCase()
  } catch (e) {
    // tasklist 本身起不来（不在 PATH 上、被拦）：当成核对不过，走回落
    return false
  }
}


// 杀掉后端进程树。child 为 null / 没有 pid 时什么都不做（后端可能已经自己退出了，
// 此时 on('exit') 已经把引用置空，重复调用必须是安全的 no-op —— stopBackend 会从
// window-all-closed 与 will-quit 各被调一次）。
function stopBackendProcess(child, {
  platform = process.platform,
  spawnFn = require('child_process').spawn,
  execFn = require('child_process').execFileSync,
  pidFile = PID_FILE,
  image = BACKEND_IMAGE,
} = {}) {
  if (!child || !child.pid) return 'noop'
  if (platform !== 'win32') {
    child.kill()
    return 'kill'
  }

  // Windows 上有两条杀法，先试精确的那条。
  //
  // 背景（这条最早是为了「关掉窗口后任务管理器里还留着 academic_backend.exe」写的）：
  // PyInstaller 的 onefile 是**两个同名进程** —— 外层 bootloader 把资源解压到
  // _MEIxxxxxx，再起一个子进程去跑真正的 Python。child.kill() 在 Windows 上只
  // TerminateProcess 直接子进程（父进程 bootloader），跑 Python 的那个会活下来，于是：
  //   ① 关掉窗口后任务管理器里还留着 academic_backend.exe；
  //   ② 它还占着 8000 端口，下次启动新后端 bind 失败并退出，但 waitForBackend 会被
  //      这个残留后端应答成 ok ⇒ 窗口照开、看起来一切正常，其实一直在跟上一轮的
  //      老进程说话，每开关一次就多留一个；
  //   ③ 父进程被强杀，它本该做的 _MEI 清理没跑，临时目录越堆越多。
  //
  // 当时用 `taskkill /T /F` 一次消掉这三个 —— 但 `/T` 是**连坐**：它同样打死
  // bootloader，于是 ③ 只是不再变严重，**已经留下的那份也没被清理**，而且每次关闭
  // 仍然新留一份（实测约 28 MB）。真正的解法是让 bootloader 活着走完它的收尾：
  // 只杀内层那一个 pid（它一退，bootloader 就自己清理并退出），① ② ③ 一起消失。
  const pid = readPidFile(pidFile)
  if (pid && isBackendAlive(pid, execFn, image)) {
    spawnFn('taskkill', ['/PID', String(pid), '/F'], { windowsHide: true })
    // 用完就删：留着它没有用处（下次启动会覆盖成新值），而一个陈旧的文件在
    // 「pid 被复用」那条路上多一次核对，少一次机会。
    try { fs.unlinkSync(pidFile) } catch (e) {}
    return 'taskkill-pid'
  }

  // 回落。触发它的四种情况：pid 文件不存在 / 读不出 / 内容不是一个正整数 /
  // tasklist 核对不过（镜像名对不上，或进程已经不在了）。**宁可留那份解压目录，
  // 也不能关不掉后端** —— 留残渣是磁盘问题，关不掉是「端口被占、下次启动跟老进程
  // 说话」，后者用户完全看不出来。
  //
  // 第五种情况（关了内层 pid 而 bootloader 在数秒内没退出）**刻意不做**：这一路在
  // app 退出途中被调用，任何定时器都活不到触发；而它不退出也不危险 —— 端口是内层那个
  // 进程持有的，内层一死端口就释放了，此时既不会有人应答 8000 造成假活，下次启动也会
  // 如实报「后端起不来」。真遇到就靠这一条 /T /F 之外的兜底：任务管理器里手动结束。
  spawnFn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true })
  return 'taskkill-tree'
}

module.exports = { stopBackendProcess, PID_FILE }
