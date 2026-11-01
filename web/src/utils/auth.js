/**
 * 前端认证工具函数
 * - Token 管理（localStorage）
 * - 用户信息缓存
 * - 认证 fetch 封装
 */

const TOKEN_KEY = 'ner_api_token'
const USER_KEY = 'ner_api_user'
const AUTH_ENABLED_KEY = 'ner_api_auth_enabled'
const FEATURES_KEY = 'ner_api_features'

// 功能模块开关默认值：旧后端响应无 features 字段时视为全部开启
const DEFAULT_FEATURES = { ner: true, attack: true, ti: true }

// 无认证模式下使用的 mock admin 用户
const MOCK_ADMIN_USER = {
  id: 1,
  username: 'admin',
  is_admin: true,
  limit_5h: 0,
  limit_weekly: 0,
  is_active: true,
}

// ─── Token 管理 ──────────────────────────────────────────────────────────────

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token)
}

export function removeToken() {
  localStorage.removeItem(TOKEN_KEY)
}

// ─── 用户信息 ────────────────────────────────────────────────────────────────

export function getUser() {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

export function setUser(user) {
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function removeUser() {
  localStorage.removeItem(USER_KEY)
}

// ─── 状态检查 ────────────────────────────────────────────────────────────────

export function isLoggedIn() {
  return !!getToken()
}

export function isAdmin() {
  const user = getUser()
  return user?.is_admin === true
}

// ─── 认证开关 ────────────────────────────────────────────────────────────────

/**
 * 从 sessionStorage 读取认证是否开启。
 * null 表示尚未初始化（需要先调用 fetchAuthConfig）。
 */
export function isAuthEnabled() {
  const v = sessionStorage.getItem(AUTH_ENABLED_KEY)
  if (v === null) return true // 默认 true，防止未初始化时直接跳过登录页
  return v !== 'false'
}

/**
 * 从后端拉取 /auth/config 并缓存到 sessionStorage。
 * @returns {Promise<boolean>} auth_enabled 值
 */
export async function fetchAuthConfig() {
  try {
    const res = await fetch('/auth/config')
    if (res.ok) {
      const data = await res.json()
      const enabled = data.auth_enabled !== false
      sessionStorage.setItem(AUTH_ENABLED_KEY, String(enabled))
      // 功能模块开关（可能缺失，旧后端无此字段）
      if (data.features && typeof data.features === 'object') {
        sessionStorage.setItem(FEATURES_KEY, JSON.stringify(data.features))
      } else {
        sessionStorage.removeItem(FEATURES_KEY)
      }
      return enabled
    }
  } catch (e) {
    console.warn('获取 auth/config 失败，默认开启认证:', e)
  }
  sessionStorage.setItem(AUTH_ENABLED_KEY, 'true')
  return true
}

/**
 * 从 sessionStorage 读取功能模块开关。
 * 缓存缺失或字段不完整时，对应功能缺省视为开启。
 * @returns {{ ner: boolean, attack: boolean, ti: boolean }}
 */
export function getFeatures() {
  const raw = sessionStorage.getItem(FEATURES_KEY)
  if (!raw) return { ...DEFAULT_FEATURES }
  try {
    const cached = JSON.parse(raw)
    return {
      ner:    cached.ner !== false,
      attack: cached.attack !== false,
      ti:     cached.ti !== false,
    }
  } catch {
    return { ...DEFAULT_FEATURES }
  }
}

/**
 * 进入无认证模式：写入 mock admin 用户，无需 token。
 */
export function initNoAuth() {
  setUser(MOCK_ADMIN_USER)
}

/**
 * 退出无认证模式：清理 mock 用户（认证重新开启时调用）。
 */
export function clearNoAuth() {
  removeUser()
  removeToken()
}


// ─── 登录 ────────────────────────────────────────────────────────────────────

/**
 * 使用用户名 + 密码登录，成功后自动保存 token 与用户信息。
 * @returns {Promise<object>} 登录成功的用户信息
 * @throws {Error} 登录失败时抛出，message 为后端返回的错误详情
 */
export async function loginWithCredentials(username, password) {
  const res = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })

  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(data.detail || '登录失败')
  }

  setToken(data.access_token)
  setUser(data.user)
  return data.user
}

// ─── 退出登录 ────────────────────────────────────────────────────────────────

export function logout() {
  removeToken()
  removeUser()
  // 触发全局事件让 App.vue 响应
  window.dispatchEvent(new Event('auth-logout'))
}

// ─── 认证 Fetch 封装 ─────────────────────────────────────────────────────────

/**
 * 封装 fetch，自动附加 Authorization header。
 * 如果收到 401 响应，自动清除 token 并触发登出。
 */
export async function authFetch(url, options = {}) {
  const token = getToken()
  const headers = { ...options.headers }

  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  // 如果 body 是对象且没有设置 content-type，自动 JSON 序列化
  let body = options.body
  if (body && typeof body === 'object' && !(body instanceof FormData) && !(body instanceof Blob)) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(body)
  }

  const response = await fetch(url, {
    ...options,
    headers,
    body,
  })

  // 401 Unauthorized → 自动登出
  if (response.status === 401) {
    logout()
    throw new Error('认证已过期，请重新登录')
  }

  return response
}
