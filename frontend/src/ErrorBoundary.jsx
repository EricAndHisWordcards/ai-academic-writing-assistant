import React from 'react'

// 渲染期异常的兜底。**这个文件是本项目里唯一该出现 componentDidCatch 的地方** ——
// 再多一处就是两个地方各自决定「出错了怎么办」，而用户看到的会是哪一种，取决于
// 异常恰好落在谁的子树里。
//
// 为什么非要有它：本轮之前全树一个 boundary 都没有，于是一个渲染期异常（最典型的是
// 漏了一行 import、某个字段名拼错）会把**整页**打成一屏空白。这在开发机上还能看控制台，
// 而打包版的用户没有控制台可看 —— 他看到的只有一个白窗口，唯一的结论是「这软件坏了」。
//
// 分两层挂载（见 main.jsx 与 App.jsx），职责不同：
//   - 外层包住整个 <App />：接住顶栏、侧栏在内的一切，最后一道。
//   - 内层只包正文区：顶栏与左侧项目列表还在，用户能切项目、能重试，而不是整个应用消失。
//
// 复位判据**只有一处**：内层那个 `key` 取项目 id（见 App.jsx），切项目即复位。
// 没有它的话，一次异常之后切到别的项目仍停在错误卡片上 —— 看起来像「这软件坏了」，
// 而实际只是「上一个项目的这一次渲染坏了」。外层不设 key：它复位等于刷新整页，
// 那件事交给用户自己按「重试」。
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // 留在控制台里（开发机与打包版的影响面不同：打包版用户看不到它，所以界面上
    // 另给了「复制错误详情」这条通道）。刻意不往上报、不写文件 —— 个人使用，
    // 没有接收方，而把栈写进用户目录只是多一处需要清理的东西。
    console.error('渲染出错：', error, info?.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    // 详情文本：用户拿不到控制台，这段是他把信息交给开发者的唯一通道。
    // 拼的是 error.stack 而不是 message —— 消息常常只是一句「Cannot read
    // properties of undefined」，定位不到是哪一行。
    const detail = [
      String(error?.stack || error || ''),
      this.props.label ? `（出错位置：${this.props.label}）` : '',
    ].filter(Boolean).join('\n')

    return (
      <div className="card error-boundary" role="alert">
        <h2>这一部分没能显示出来</h2>
        {/* 不猜原因，并列穷举三种可能 —— 这里的代码拿不到「为什么」，
            而说出一个猜错的原因会让用户去查一个不存在的方向。 */}
        <p className="muted">
          可能的原因：项目数据读取不完整、浏览器渲染出错，或者软件版本与数据不匹配。
        </p>
        <div className="actions">
          <button
            className="btn btn-primary"
            onClick={() => this.setState({ error: null })}
          >
            重试
          </button>
          <button
            className="btn"
            onClick={() => { navigator.clipboard?.writeText(detail) }}
          >
            复制错误详情
          </button>
        </div>
        {/* 出路必须写明**地点**与**按钮名**，否则等于没说。 */}
        <p className="muted">
          如果重试还是不行：在左侧「我的论文项目」里换一个项目打开；
          换哪个都一样的话，关掉软件重新打开。
        </p>
        <details>
          <summary>错误详情</summary>
          <pre className="error-detail">{detail}</pre>
        </details>
      </div>
    )
  }
}
