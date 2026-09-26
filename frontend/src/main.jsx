import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import './index.css'

// 外层 boundary：包住整个 App，接住顶栏、侧栏在内的一切渲染期异常。**这是最后一道**
// —— 没有它的话，App 子树里任何一个异常都会让整页变白（打包版用户看不到控制台，
// 只能看到一个白窗口）。App.jsx 里正文区另有一层，那一层保住的是「顶栏与项目列表还在」。
//
// `label` 只进错误详情，不影响判据：它告诉用户（和拿到详情的人）异常大致出在哪一层，
// 因为内层那条会先说它是哪一层出的事。
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary label="整个应用">
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
