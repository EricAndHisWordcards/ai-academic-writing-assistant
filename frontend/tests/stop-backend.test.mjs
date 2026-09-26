// stop-backend.cjs 的 node:test 用例。坏掉的表现是「关掉应用后进程管理器里还留着
// academic_backend.exe」「再打开时新后端 bind 失败却被残留后端应答成正常」，都是不看
// 日志、不看任务管理器就发现不了的静默缺陷。
//
// 每条用例都**显式注入 pidFile**：默认值是用户目录里那个真实的文件，不注入的话断言
// 会跟着这台机器上有没有装过桌面版而变 —— 那种测试在本机绿、在别处红，且红的原因与
// 被测代码无关。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { stopBackendProcess } = require('../electron/stop-backend.cjs')

// 假 spawn：只记录被调用的样子，不真起进程。
function makeSpawn() {
  const calls = []
  const spawnFn = (cmd, args, opts) => { calls.push({ cmd, args, opts }) }
  return { calls, spawnFn }
}

// 一个临时的 pid 文件路径。写成文件则返回该路径（内容为 text）；不传 text 则不创建。
function tempPidFile(text) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'stop-backend-'))
  const file = path.join(dir, 'backend.pid')
  if (text !== undefined) fs.writeFileSync(file, text)
  return file
}

// 假 tasklist：命中时照着真 tasklist 的 CSV 形态回一行，未命中时回那句提示语。
function makeTasklist({ matches }) {
  const calls = []
  const execFn = (cmd, args) => {
    calls.push({ cmd, args })
    return matches
      ? `"academic_backend.exe","4242","Console","1","12,345 K"\n`
      : '信息: 没有运行的任务匹配指定标准。\n'
  }
  return { calls, execFn }
}

test('Windows：pid 文件正常且镜像名核对通过 → 只杀那一个 pid，不带 /T', () => {
  // 这一条是这个改动**本身**：不带 /T，bootloader 才能活着走完它的 _MEI 清理。
  const { calls, spawnFn } = makeSpawn()
  const { execFn } = makeTasklist({ matches: true })
  const pidFile = tempPidFile('4242')

  const mode = stopBackendProcess({ pid: 1000 }, {
    platform: 'win32', spawnFn, execFn, pidFile,
  })

  assert.equal(mode, 'taskkill-pid')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].cmd, 'taskkill')
  assert.deepEqual(calls[0].args, ['/PID', '4242', '/F'])
  assert.equal(calls[0].opts.windowsHide, true)
  // 用的是文件里的 pid，不是 Electron 那个直接子进程（bootloader）的 pid ——
  // 用错那个就等于退回今天这条 /T 路径，整个改动白做。
  assert.notEqual(calls[0].args[1], '1000')
  // 用完删掉：下次启动会写新的，留着只会让「pid 被复用」那条路上多一次误判机会
  assert.equal(fs.existsSync(pidFile), false)
})

test('Windows：pid 文件不存在 → 回落 /T /F', () => {
  // 回落的那条就是改动前一直在跑的那条。它必须原样在 —— 宁可留残渣，不能关不掉。
  const { calls, spawnFn } = makeSpawn()
  const { execFn } = makeTasklist({ matches: true })
  const pidFile = tempPidFile()   // 不创建

  const mode = stopBackendProcess({ pid: 1000 }, {
    platform: 'win32', spawnFn, execFn, pidFile,
  })

  assert.equal(mode, 'taskkill-tree')
  assert.deepEqual(calls[0].args, ['/PID', '1000', '/T', '/F'])
})

test('Windows：pid 指向的镜像名不是后端 → 回落 /T /F', () => {
  // 进程号会被复用。一个陈旧 pid 落到别的程序上时照杀就是误杀 —— 所以镜像名要核对。
  // 源码态直跑（python.exe）走的也是这一条。
  const { calls, spawnFn } = makeSpawn()
  const execFn = () => '"python.exe","4242","Console","1","12,345 K"\n'
  const pidFile = tempPidFile('4242')

  const mode = stopBackendProcess({ pid: 1000 }, {
    platform: 'win32', spawnFn, execFn, pidFile,
  })

  assert.equal(mode, 'taskkill-tree')
  assert.deepEqual(calls[0].args, ['/PID', '1000', '/T', '/F'])
})

test('Windows：tasklist 没命中（进程已不在）→ 回落 /T /F', () => {
  // 这一条专钉「判据不能是"输出非空"」：没命中时 tasklist 也会往 stdout 写一句提示语
  // （中文系统上是「信息: 没有运行的任务匹配指定标准。」）。按非空判定的话，每一次
  // 核对都会被放行 —— 那等于没有核对。
  const { calls, spawnFn } = makeSpawn()
  const { execFn } = makeTasklist({ matches: false })
  const pidFile = tempPidFile('4242')

  const mode = stopBackendProcess({ pid: 1000 }, {
    platform: 'win32', spawnFn, execFn, pidFile,
  })

  assert.equal(mode, 'taskkill-tree')
  assert.deepEqual(calls[0].args, ['/PID', '1000', '/T', '/F'])
})

test('Windows：pid 文件内容不是一个正整数 → 回落 /T /F，不抛', () => {
  // 空文件、半截写入、被别的东西覆写成文本，都得走回落而不是抛。
  const { calls, spawnFn } = makeSpawn()
  const { execFn } = makeTasklist({ matches: true })

  for (const junk of ['', '  ', 'abc', '0', '-1', '12.5', '99999999999999999999']) {
    const pidFile = tempPidFile(junk)
    const mode = stopBackendProcess({ pid: 1000 }, {
      platform: 'win32', spawnFn, execFn, pidFile,
    })
    assert.equal(mode, 'taskkill-tree', `内容为 ${JSON.stringify(junk)} 时应回落`)
  }
  // 每一次都回落了 ⇒ 一次精确杀都没发生
  assert.equal(calls.filter((c) => !c.args.includes('/T')).length, 0)
})

test('Windows：tasklist 起不来（抛异常）→ 回落 /T /F，不抛', () => {
  // 环境里没有 tasklist（PATH 被裁剪、被安全软件拦）时，不能让关闭流程断在这里 ——
  // 这个函数抛出去的话，main.cjs 那个 try/catch 会吞掉，代价是后端整个没被收掉。
  const { calls, spawnFn } = makeSpawn()
  const execFn = () => { throw new Error('tasklist 起不来') }
  const pidFile = tempPidFile('4242')

  const mode = stopBackendProcess({ pid: 1000 }, {
    platform: 'win32', spawnFn, execFn, pidFile,
  })

  assert.equal(mode, 'taskkill-tree')
  assert.deepEqual(calls[0].args, ['/PID', '1000', '/T', '/F'])
})

test('非 Windows：回落到 child.kill()，不起 taskkill', () => {
  const { calls, spawnFn } = makeSpawn()
  let killed = 0
  const mode = stopBackendProcess(
    { pid: 4242, kill: () => { killed++ } },
    { platform: 'linux', spawnFn },
  )
  assert.equal(mode, 'kill')
  assert.equal(killed, 1)
  assert.equal(calls.length, 0)
})

test('后端已自行退出（child 为 null）：no-op 且不抛', () => {
  const { calls, spawnFn } = makeSpawn()
  // stopBackend 会被 window-all-closed 与 will-quit 各调一次，第二次必须安全
  assert.equal(stopBackendProcess(null, { platform: 'win32', spawnFn }), 'noop')
  assert.equal(calls.length, 0)
})

test('child 存在但没有 pid：no-op，别拿 undefined 去 taskkill', () => {
  const { calls, spawnFn } = makeSpawn()
  assert.equal(stopBackendProcess({}, { platform: 'win32', spawnFn }), 'noop')
  assert.equal(calls.length, 0)
})
