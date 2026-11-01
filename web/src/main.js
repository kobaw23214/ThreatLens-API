import { createApp } from 'vue'
import App from './App.vue'
import { getToken, logout, fetchAuthConfig, initNoAuth, isAuthEnabled } from './utils/auth.js'

// ── 启动时拉取认证配置 ────────────────────────────────────────────────────────
// 必须在 createApp 之前完成，以便 App.vue 初始化时已有正确状态。
const authEnabled = await fetchAuthConfig()
if (!authEnabled) {
  // 无认证模式：写入 mock admin 用户，让 App.vue 直接进入 Dashboard
  initNoAuth()
}

// ── 全局拦截 fetch：自动附加 Authorization header ───────────────────────────
// 这样所有现有组件的 fetch 调用都会自动携带 JWT token，
// 无需逐个修改每个 .vue 文件。
const _originalFetch = window.fetch
window.fetch = async function (url, options = {}) {
  const token = getToken()
  if (token) {
    const headers = new Headers(options.headers || {})
    // 不覆盖已有的 Authorization header
    if (!headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    options = { ...options, headers }
  }

  const response = await _originalFetch.call(window, url, options)

  // 401 → 自动登出（但跳过 /auth/login 和 /auth/config 请求本身，
  //               以及无认证模式下的所有请求）
  if (
    response.status === 401 &&
    !String(url).includes('/auth/login') &&
    !String(url).includes('/auth/config') &&
    isAuthEnabled()
  ) {
    logout()
  }

  return response
}

createApp(App).mount('#app')

