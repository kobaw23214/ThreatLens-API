<script setup>
import { ref, watch, onMounted, onUnmounted, provide, computed } from 'vue'
import QueueStatus    from './components/QueueStatus.vue'
import QueueTaskListOverlay from './components/QueueTaskListOverlay.vue'
import ModelStats     from './components/ModelStats.vue'
import EntityGraph    from './components/EntityGraph.vue'
import ApiPlayground  from './components/ApiPlayground.vue'
import CallbackFailedManagement from './components/CallbackFailedManagement.vue'
import CallbackStats from './components/CallbackStats.vue'
import CallbackSuccessLogs from './components/CallbackSuccessLogs.vue'
import LoginPage     from './components/LoginPage.vue'
import UserPanel     from './components/UserPanel.vue'
import AdminPanel    from './components/AdminPanel.vue'
import ManualViewer  from './components/ManualViewer.vue'
import TiPlayground  from './components/TiPlayground.vue'
import { isLoggedIn, isAdmin, getUser, logout, setUser, authFetch, loginWithCredentials, isAuthEnabled, getFeatures } from './utils/auth.js'

// ─── 登录状态 ────────────────────────────────────────────────────────────────
// 无认证模式：不需要 token，直接视为已登录（getUser() 已被 initNoAuth 写入 mock admin）
const authEnabled = isAuthEnabled()
// 功能模块开关（main.js 已在 createApp 前拉取 /auth/config 并写入缓存）
const features = getFeatures()
const loggedIn = ref(!authEnabled || isLoggedIn())
const currentUser = ref(getUser())

function handleLoginSuccess(user) {
  loggedIn.value = true
  currentUser.value = user
}

function handleLogout() {
  // 无认证模式下不允许退出
  if (!authEnabled) return
  logout()
  loggedIn.value = false
  currentUser.value = null
  activeTab.value = 'queue'
}

// 监听全局 auth-logout 事件（由 authFetch 401 触发）
window.addEventListener('auth-logout', () => {
  loggedIn.value = false
  currentUser.value = null
})

// ─── URL 参数自动登录（用于 iframe 集成，免登录页）────────────────────────────
// 访问 http://host/?user=admin&password=xxx 时自动登录，登录后立即清除 URL 中的凭据。
// 拆分 hash 为「路由部分」与「查询部分」。
// 集成方可能把凭据写在 hash 之后（#/iframe/{id}?user=a&password=b），
// 此时必须把 ?query 剥掉，否则会被当成 task_id 的一部分拼进接口 URL。
function splitHash() {
  const raw = location.hash.replace(/^#/, '')
  const qIdx = raw.indexOf('?')
  return {
    route: (qIdx === -1 ? raw : raw.slice(0, qIdx)).replace(/^\/+|\/+$/g, ''),
    query: qIdx === -1 ? '' : raw.slice(qIdx + 1),
  }
}

const CRED_KEYS = ['user', 'username', 'password', 'pwd']

// 凭据允许出现在 location.search 或 hash 的查询串中，两者都兼容
function getAutoLoginCredentials() {
  const search = new URLSearchParams(location.search)
  const hashQuery = new URLSearchParams(splitHash().query)
  const pick = (...keys) => {
    for (const k of keys) {
      const v = search.get(k) || hashQuery.get(k)
      if (v) return v
    }
    return null
  }
  const username = pick('user', 'username')
  const password = pick('password', 'pwd')
  if (username && password) return { username, password }
  return null
}

const autoLoginCreds = getAutoLoginCredentials()
// 存在凭据且尚未登录时，先展示加载态，避免登录页一闪而过
const autoLoggingIn = ref(!loggedIn.value && !!autoLoginCreds)

// 从地址栏移除账号密码，避免凭据残留在浏览器历史 / 引用来源中
function stripCredentialsFromUrl() {
  const params = new URLSearchParams(location.search)
  CRED_KEYS.forEach((k) => params.delete(k))
  const qs = params.toString()

  // hash 里夹带的凭据同样要清理，只保留路由部分与其它无关参数
  const { route, query } = splitHash()
  const hashParams = new URLSearchParams(query)
  CRED_KEYS.forEach((k) => hashParams.delete(k))
  const hashQs = hashParams.toString()
  const hash = location.hash ? `#${route}${hashQs ? `?${hashQs}` : ''}` : ''

  const newUrl = location.pathname + (qs ? `?${qs}` : '') + hash
  history.replaceState(null, '', newUrl)
}

async function tryAutoLogin() {
  if (!autoLoginCreds) return
  // 已登录则只清理 URL，不重复登录
  if (loggedIn.value) {
    stripCredentialsFromUrl()
    return
  }
  try {
    const user = await loginWithCredentials(autoLoginCreds.username, autoLoginCreds.password)
    handleLoginSuccess(user)
    isAdminUser.value = isAdmin()
  } catch (e) {
    console.error('自动登录失败:', e.message)
  } finally {
    autoLoggingIn.value = false
    stripCredentialsFromUrl()
  }
}

// ─── 从 URL hash 或 path 恢复标签页状态 ──────────────────────────────────────────────
// 功能相关 tab → 功能开关的映射；未列出的 tab（queue/graph/iframe/callback/user/admin/manual）不受开关影响
const TAB_FEATURE = {
  'stats-ner': 'ner', 'stats-attack': 'attack', 'stats-ti': 'ti',
  'playground': 'ner', 'attack': 'attack', 'ti-console': 'ti',
  'graph-ner': 'ner', 'graph-attack': 'attack', 'graph-ti': 'ti',
}

function parseHash() {
  // 1. 优先检测 pathname 是否匹配 /iframe/{task_id}
  const pathname = location.pathname
  const pathParts = pathname.split('/').filter(Boolean)
  const iframeIdx = pathParts.indexOf('iframe')
  if (iframeIdx !== -1 && pathParts[iframeIdx + 1]) {
    return {
      tab: 'iframe',
      sub: '',
      id: pathParts[iframeIdx + 1]
    }
  }

  // 2. 检查 hash 是否匹配 #/iframe/{task_id} 或其他标签（已剥离 hash 中的查询串）
  const hash = splitHash().route
  if (!hash) return { tab: 'queue', sub: 'stats', id: '' }
  const [tab, sub] = hash.split('/')
  
  if (tab === 'iframe' && sub) {
    return {
      tab: 'iframe',
      sub: '',
      id: sub
    }
  }

  const validTabs = ['queue', 'stats-ner', 'stats-attack', 'stats-ti', 'playground', 'attack', 'ti-console', 'graph', 'graph-ner', 'graph-attack', 'graph-ti', 'callback', 'user', 'admin', 'manual']
    // 被禁用功能对应的 tab 从白名单剔除（手改 hash 也进不去），落到默认 queue
    .filter(t => TAB_FEATURE[t] === undefined || features[TAB_FEATURE[t]])
  const validSubs = ['stats', 'failed', 'success']
  // 旧版 #stats 锚点兼容：落到 NER 统计子页
  const normTab = tab === 'stats' ? 'stats-ner' : tab
  return {
    tab: validTabs.includes(normTab) ? normTab : 'queue',
    sub: validSubs.includes(sub) ? sub : 'stats',
    id: ''
  }
}

const initial = parseHash()
const activeTab      = ref(initial.tab)
const callbackSubTab = ref(initial.sub)
const graphTaskId    = ref(initial.id)
const isIframeMode   = computed(() => activeTab.value === 'iframe')

// 标签页变化时同步到 URL hash
function updateHash() {
  if (isIframeMode.value) return // iframe 模式下不修改 hash
  const hash = activeTab.value === 'callback'
    ? `${activeTab.value}/${callbackSubTab.value}`
    : activeTab.value
  history.replaceState(null, '', `#${hash}`)
}

watch(activeTab, updateHash)
watch(callbackSubTab, updateHash)

// 浏览器前进/后退/hash改变时恢复状态
const handleRouting = () => {
  const { tab, sub, id } = parseHash()
  activeTab.value = tab
  callbackSubTab.value = sub
  if (tab === 'iframe') {
    graphTaskId.value = id
  }
}

window.addEventListener('hashchange', handleRouting)
window.addEventListener('popstate', handleRouting)

function openGraph(taskId) {
  graphTaskId.value = taskId
  activeTab.value   = 'graph'
}

// ─── 队列任务列表全屏 Overlay ─────────────────────────────────────────────────
// queueDetail: null | { type: 'extract'|'ti'|'callback', tab?: 'pending'|'failed'|'success' }
const queueDetail = ref(null)

function openQueueDetail(payload) {
  queueDetail.value = payload
}

function closeQueueDetail() {
  queueDetail.value = null
}

// 从队列任务列表跳转「解析详情」: 先关闭 overlay 再切 Tab
function openGraphFromQueue(taskId) {
  closeQueueDetail()
  openGraph(taskId)
}

const sseData = ref({ queue_size: 0, workers: [], timestamp: '' })
let eventSource = null

const isAdminUser = ref(isAdmin())
const callbackUsers = ref([])
const selectedCallbackUserId = ref('')

async function fetchCallbackUsers() {
  if (!isAdminUser.value) return
  try {
    const res = await authFetch('/admin/users?page_size=1000')
    if (res.ok) {
      const data = await res.json()
      callbackUsers.value = data.items || []
    }
  } catch (e) {
    console.error('获取回调用户列表失败', e)
  }
}

watch(activeTab, async (newTab) => {
  if (newTab === 'callback' && isAdminUser.value && callbackUsers.value.length === 0) {
    await fetchCallbackUsers()
  }
})

function connectSSE() {
  eventSource = new EventSource('/events')
  eventSource.onmessage = (e) => {
    const data = JSON.parse(e.data)
    if (!data.heartbeat) sseData.value = data
  }
  eventSource.onerror = () => {
    eventSource.close()
    setTimeout(connectSSE, 3000)
  }
}

onMounted(async () => {
  await tryAutoLogin()
  connectSSE()
  if (activeTab.value === 'callback' && isAdminUser.value) {
    await fetchCallbackUsers()
  }
})
onUnmounted(() => eventSource?.close())
provide('sseData', sseData)

const navItems = computed(() => {
  // 按功能开关过滤子项；子项全部被过滤的父级下拉不显示
  const featureChildren = (children) => children.filter(c => features[TAB_FEATURE[c.key]])
  const items = [
    { key: 'queue',      label: '队列状态' },
    {
      key: 'task-stats',
      label: '任务统计',
      children: featureChildren([
        { key: 'stats-ner',    label: 'NER 模型统计' },
        { key: 'stats-attack', label: 'ATT&CK 模型统计' },
        { key: 'stats-ti',     label: '联网解析统计' },
      ])
    },
    {
      key: 'api-console',
      label: 'API 调用台',
      children: featureChildren([
        { key: 'playground', label: 'NER 调用台' },
        { key: 'attack',     label: 'ATT&CK 调用台' },
        { key: 'ti-console', label: '联网情报调用台' },
      ])
    },
    { key: 'graph-parent',
      label: '解析详情',
      children: featureChildren([
        { key: 'graph-ner',    label: 'NER 解析' },
        { key: 'graph-attack', label: 'ATT&CK 解析' },
        { key: 'graph-ti',     label: '联网情报解析' },
      ])
    },
    { key: 'callback',   label: '回调结果' },
    { key: 'manual',     label: '使用手册' },
    { key: 'user',       label: '用户' },
  ].filter(item => !item.children || item.children.length > 0)
  if (currentUser.value?.is_admin) {
    items.push({ key: 'admin', label: '管理' })
  }
  return items
})

// 父项是否处于激活态（子项之一被选中）
function isParentActive(item) {
  if (!item.children) return false
  return item.children.some(c => c.key === activeTab.value)
}

// 下拉菜单状态
const openDropdown = ref(null)
function openMenu(key)  { openDropdown.value = key }
function closeMenu()    { openDropdown.value = null }
</script>

<template>
  <!-- 自动登录中：展示加载态，避免登录页闪烁 -->
  <div v-if="autoLoggingIn" class="auto-login-loading">
    <div class="auto-login-spinner"></div>
    <p>正在登录…</p>
  </div>

  <!-- 未登录：显示登录页 -->
  <LoginPage v-else-if="!loggedIn" @login-success="handleLoginSuccess" />

  <!-- 已登录：显示 Dashboard -->
  <div v-else class="app">
    <header v-if="!isIframeMode">
      <div class="brand">
        <svg class="brand-icon" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="hbg" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" style="stop-color:#5470c6"/>
              <stop offset="100%" style="stop-color:#3ba272"/>
            </linearGradient>
          </defs>
          <rect width="32" height="32" rx="6" fill="url(#hbg)"/>
          <text x="16" y="22" text-anchor="middle" font-family="Arial,sans-serif" font-weight="700" font-size="16" fill="#fff">N</text>
          <circle cx="25" cy="7" r="4" fill="#f59e0b"/>
          <path d="M23.5 7 L24.5 8 L27 5.5" stroke="#fff" stroke-width="1.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <h1>ThreatLens Dashboard</h1>
      </div>
      <nav>
        <template v-for="item in navItems" :key="item.key">
          <!-- 普通菜单项 -->
          <button
            v-if="!item.children"
            :class="{ active: activeTab === item.key }"
            @click="activeTab = item.key"
          >
            <svg class="nav-icon" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
              <!-- 队列状态 -->
              <template v-if="item.key === 'queue'">
                <rect x="2" y="3" width="16" height="3" rx="1" fill="currentColor" opacity=".9"/>
                <rect x="2" y="8.5" width="12" height="3" rx="1" fill="currentColor" opacity=".6"/>
                <rect x="2" y="14" width="8" height="3" rx="1" fill="currentColor" opacity=".35"/>
              </template>
              <!-- 回调结果 -->
              <template v-if="item.key === 'callback'">
                <path d="M4 4h8l4 4v10a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1z" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <polyline points="12 4 12 8 16 8" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <path d="M7 12l2 2 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
              </template>
              <!-- 使用手册 -->
              <template v-if="item.key === 'manual'">
                <path d="M3 4a1 1 0 011-1h5v14H4a1 1 0 01-1-1V4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>
                <path d="M17 4a1 1 0 00-1-1h-5v14h5a1 1 0 001-1V4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>
                <line x1="5.5" y1="6.5" x2="7.5" y2="6.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>
                <line x1="12.5" y1="6.5" x2="14.5" y2="6.5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>
              </template>
              <!-- 用户 -->
              <template v-if="item.key === 'user'">
                <circle cx="10" cy="7" r="3.5" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <path d="M3 18c0-4 3.5-7 7-7s7 3 7 7" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
              </template>
              <!-- 管理 -->
              <template v-if="item.key === 'admin'">
                <path d="M10 2l2 2-1.5 1.5L12 7l-2 2-1.5-1.5L7 9l-2-2 1.5-1.5L5 4l2-2 1.5 1.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>
                <circle cx="10" cy="10" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/>
                <path d="M4 16c0-3 2.7-5.5 6-5.5s6 2.5 6 5.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
              </template>
            </svg>
            <span>{{ item.label }}</span>
          </button>

          <!-- 有子菜单的父项 -->
          <div
            v-else
            class="nav-dropdown"
            :class="{ active: isParentActive(item) }"
            @mouseenter="openMenu(item.key)"
            @mouseleave="closeMenu()"
          >
            <button class="nav-dropdown-trigger" :class="{ active: isParentActive(item) }">
              <svg class="nav-icon" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
                <!-- API 调用台 -->
                <template v-if="item.key === 'api-console'">
                  <rect x="2" y="2" width="16" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
                  <path d="M6 7l3 3-3 3" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
                  <line x1="11" y1="13" x2="15" y2="13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                </template>
                <!-- 任务统计 -->
                <template v-if="item.key === 'task-stats'">
                  <rect x="2" y="12" width="3" height="6" rx="1" fill="currentColor"/>
                  <rect x="7" y="8" width="3" height="10" rx="1" fill="currentColor" opacity=".7"/>
                  <rect x="12" y="4" width="3" height="14" rx="1" fill="currentColor" opacity=".5"/>
                  <line x1="1" y1="18" x2="19" y2="18" stroke="currentColor" stroke-width="1.2" opacity=".3"/>
                </template>
                <!-- 解析详情 -->
                <template v-if="item.key === 'graph-parent'">
                  <circle cx="10" cy="6" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/>
                  <circle cx="5" cy="15" r="2.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
                  <circle cx="16" cy="14" r="2.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
                  <line x1="8.5" y1="8.5" x2="6" y2="13" stroke="currentColor" stroke-width="1.2"/>
                  <line x1="11.5" y1="8.5" x2="15" y2="12" stroke="currentColor" stroke-width="1.2"/>
                </template>
              </svg>
              <span>{{ item.label }}</span>
              <!-- 下拉箭头 -->
              <svg class="dropdown-arrow" :class="{ open: openDropdown === item.key }" viewBox="0 0 10 6" xmlns="http://www.w3.org/2000/svg">
                <path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            <div class="nav-dropdown-menu" v-show="openDropdown === item.key">
              <button
                v-for="child in item.children"
                :key="child.key"
                @click="activeTab = child.key; closeMenu()"
                :class="{ active: activeTab === child.key }"
              >
                <svg class="nav-icon" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
                  <!-- NER 调用台 -->
                  <template v-if="child.key === 'playground'">
                    <rect x="2" y="2" width="16" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
                    <path d="M6 7l3 3-3 3" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
                    <line x1="11" y1="13" x2="15" y2="13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                  </template>
                  <!-- ATT&CK 调用台 -->
                  <template v-if="child.key === 'attack'">
                    <path d="M10 2l7 3v5c0 4-3 7-7 8-4-1-7-4-7-8V5l7-3z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
                    <path d="M7 10l2 2 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
                  </template>
                  <!-- 联网情报调用台 -->
                  <template v-if="child.key === 'ti-console'">
                    <circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="1.4"/>
                    <circle cx="10" cy="10" r="3" fill="none" stroke="currentColor" stroke-width="1.2" opacity=".6"/>
                    <circle cx="10" cy="10" r="1.2" fill="currentColor"/>
                    <line x1="14" y1="6" x2="17" y2="3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
                    <circle cx="17.5" cy="2.5" r="1.2" fill="currentColor"/>
                  </template>
                  <!-- NER 模型统计 -->
                  <template v-if="child.key === 'stats-ner'">
                    <rect x="2" y="12" width="3" height="6" rx="1" fill="currentColor"/>
                    <rect x="7" y="8" width="3" height="10" rx="1" fill="currentColor" opacity=".7"/>
                    <rect x="12" y="4" width="3" height="14" rx="1" fill="currentColor" opacity=".5"/>
                  </template>
                  <!-- ATT&CK 模型统计 -->
                  <template v-if="child.key === 'stats-attack'">
                    <path d="M10 2l7 3v5c0 4-3 7-7 8-4-1-7-4-7-8V5l7-3z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
                    <rect x="8" y="10" width="1.6" height="5" rx=".8" fill="currentColor"/>
                    <rect x="11.5" y="8" width="1.6" height="7" rx=".8" fill="currentColor" opacity=".7"/>
                  </template>
                  <!-- 联网解析统计 -->
                  <template v-if="child.key === 'stats-ti'">
                    <circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="1.4"/>
                    <rect x="7" y="10" width="1.6" height="4" rx=".8" fill="currentColor"/>
                    <rect x="10.5" y="7.5" width="1.6" height="6.5" rx=".8" fill="currentColor" opacity=".7"/>
                  </template>
                  <!-- NER 解析 -->
                  <template v-if="child.key === 'graph-ner'">
                    <circle cx="10" cy="6" r="3" fill="none" stroke="currentColor" stroke-width="1.5"/>
                    <circle cx="5" cy="15" r="2.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
                    <circle cx="16" cy="14" r="2.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
                    <line x1="8.5" y1="8.5" x2="6" y2="13" stroke="currentColor" stroke-width="1.2"/>
                    <line x1="11.5" y1="8.5" x2="15" y2="12" stroke="currentColor" stroke-width="1.2"/>
                  </template>
                  <!-- ATT&CK 解析 -->
                  <template v-if="child.key === 'graph-attack'">
                    <path d="M10 2l7 3v5c0 4-3 7-7 8-4-1-7-4-7-8V5l7-3z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
                    <circle cx="10" cy="10" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/>
                  </template>
                  <!-- 联网情报解析 -->
                  <template v-if="child.key === 'graph-ti'">
                    <circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="1.4"/>
                    <circle cx="10" cy="10" r="3" fill="none" stroke="currentColor" stroke-width="1.2" opacity=".6"/>
                    <circle cx="10" cy="10" r="1.2" fill="currentColor"/>
                  </template>
                </svg>
                {{ child.label }}
              </button>
            </div>
          </div>
        </template>
      </nav>

      <!-- 用户信息 + 退出 -->
      <div class="user-bar">
        <span class="user-name">
          <svg class="user-avatar" viewBox="0 0 16 16">
            <circle cx="8" cy="6" r="3" fill="none" stroke="currentColor" stroke-width="1.2"/>
            <path d="M2 15c0-3 2.7-5 6-5s6 2 6 5" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
          </svg>
          {{ currentUser?.username }}
        </span>
        <button v-if="authEnabled" class="logout-btn" @click="handleLogout" title="退出登录">
          <svg viewBox="0 0 16 16" class="logout-icon">
            <path d="M6 2H3a1 1 0 00-1 1v10a1 1 0 001 1h3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
            <path d="M10 11l3-3-3-3M6 8h7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>
    </header>

    <main :class="{ 'iframe-mode': isIframeMode }">
      <QueueStatus   v-if="activeTab === 'queue'" @open-queue="openQueueDetail" />
      <ModelStats    v-if="activeTab === 'stats-ner' && features.ner" key="stats-ner" section="ner" />
      <ModelStats    v-if="activeTab === 'stats-attack' && features.attack" key="stats-attack" section="attack" />
      <ModelStats    v-if="activeTab === 'stats-ti' && features.ti" key="stats-ti" section="ti" />
      <ApiPlayground v-if="activeTab === 'playground' && features.ner" key="ner-console" console-mode="ner" @open-graph="openGraph" />
      <ApiPlayground v-if="activeTab === 'attack' && features.attack" key="attack-console" console-mode="attack" @open-graph="openGraph" />
      <TiPlayground  v-if="activeTab === 'ti-console' && features.ti" @open-graph="openGraph" />
      <EntityGraph   v-if="activeTab === 'graph-ner' && features.ner" key="graph-ner" initial-category="ner" />
      <EntityGraph   v-if="activeTab === 'graph-attack' && features.attack" key="graph-attack" initial-category="attack" />
      <EntityGraph   v-if="activeTab === 'graph-ti' && features.ti" key="graph-ti" initial-category="ti" />
      <!-- 任务详情直达（队列/调用台跳转）：自动探测类别，类别 tab 随结果切换 -->
      <EntityGraph   v-if="activeTab === 'graph' || activeTab === 'iframe'" :initial-task-id="graphTaskId" :is-iframe="isIframeMode" />

      <!-- 回调结果模块 -->
      <div v-if="activeTab === 'callback'" class="callback-module">
        <div class="sub-tabs">
          <div class="sub-tab-buttons">
            <button
              :class="{ active: callbackSubTab === 'stats' }"
              @click="callbackSubTab = 'stats'"
            >
              <svg class="sub-icon" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
                <circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <path d="M9 5v4l3 2" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round"/>
              </svg>
              回调统计
            </button>
            <button
              :class="{ active: callbackSubTab === 'failed' }"
              @click="callbackSubTab = 'failed'"
            >
              <svg class="sub-icon" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
                <circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <line x1="6.5" y1="6.5" x2="11.5" y2="11.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
                <line x1="11.5" y1="6.5" x2="6.5" y2="11.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              </svg>
              回调失败
            </button>
            <button
              :class="{ active: callbackSubTab === 'success' }"
              @click="callbackSubTab = 'success'"
            >
              <svg class="sub-icon" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
                <circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-width="1.4"/>
                <path d="M6 9l2 2 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              成功日志
            </button>
          </div>

          <div v-if="isAdminUser" class="callback-user-filter">
            <span class="filter-label">用户:</span>
            <select v-model="selectedCallbackUserId" class="user-select">
              <option value="">全部</option>
              <option v-for="u in callbackUsers" :key="u.id" :value="u.id">{{ u.username }}</option>
            </select>
          </div>
        </div>
        <div class="sub-content">
          <CallbackStats v-if="callbackSubTab === 'stats'" :user-id="selectedCallbackUserId" />
          <CallbackFailedManagement v-if="callbackSubTab === 'failed'" :user-id="selectedCallbackUserId" />
          <CallbackSuccessLogs v-if="callbackSubTab === 'success'" :user-id="selectedCallbackUserId" />
        </div>
      </div>

      <!-- 用户面板 -->
      <UserPanel v-if="activeTab === 'user'" />

      <!-- 使用手册 -->
      <ManualViewer v-if="activeTab === 'manual'" />

      <!-- 管理员面板 -->
      <AdminPanel v-if="activeTab === 'admin'" @open-graph="openGraph" />

      <!-- 队列任务列表全屏 Overlay -->
      <QueueTaskListOverlay
        v-if="queueDetail"
        :queue-type="queueDetail.type"
        :initial-tab="queueDetail.tab || 'pending'"
        @close="closeQueueDetail"
        @open-graph="openGraphFromQueue"
      />
    </main>
  </div>
</template>

<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; color: #333; }
.app { width: 100%; min-height: 100vh; }

/* ── 自动登录加载态 ── */
.auto-login-loading {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  background: linear-gradient(135deg, #0f0c29 0%, #1a1a2e 40%, #16213e 100%);
  color: rgba(255, 255, 255, 0.8);
  font-size: 14px;
}
.auto-login-spinner {
  width: 36px;
  height: 36px;
  border: 3px solid rgba(255, 255, 255, 0.2);
  border-top-color: #5470c6;
  border-radius: 50%;
  animation: auto-login-spin 0.7s linear infinite;
}
@keyframes auto-login-spin {
  to { transform: rotate(360deg); }
}

/* ── Header ── */
header {
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  color: #fff;
  padding: 0 24px;
  display: flex;
  align-items: center;
  gap: 32px;
  height: 56px;
  box-shadow: 0 2px 8px rgba(0,0,0,.2);
  position: sticky;
  top: 0;
  z-index: 100;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.brand-icon {
  width: 28px;
  height: 28px;
}

header h1 {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: .3px;
  white-space: nowrap;
}

/* ── Nav ── */
nav {
  display: flex;
  gap: 4px;
  flex: 1;
  align-items: stretch;
  /* 不设 overflow，避免裁剪绝对定位的下拉菜单 */
}

nav > button {
  background: transparent;
  border: none;
  color: rgba(255,255,255,.65);
  padding: 0 16px;
  height: 56px;
  cursor: pointer;
  font-size: 13px;
  transition: all .15s;
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
  position: relative;
}

nav > button:hover {
  color: rgba(255,255,255,.9);
  background: rgba(255,255,255,.05);
}

nav > button.active {
  color: #fff;
  border-bottom-color: #5470c6;
  background: rgba(255,255,255,.08);
}

.nav-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

/* ── Dropdown 父项 ── */
.nav-dropdown {
  position: relative;
  display: flex;
  align-items: stretch;
}

.nav-dropdown-trigger {
  background: transparent;
  border: none;
  color: rgba(255,255,255,.65);
  padding: 0 14px;
  height: 56px;
  cursor: pointer;
  font-size: 13px;
  transition: all .15s;
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
}

.nav-dropdown:hover .nav-dropdown-trigger,
.nav-dropdown-trigger:hover {
  color: rgba(255,255,255,.9);
  background: rgba(255,255,255,.05);
}

.nav-dropdown-trigger.active {
  color: #fff;
  border-bottom-color: #5470c6;
  background: rgba(255,255,255,.08);
}

.dropdown-arrow {
  width: 10px;
  height: 6px;
  flex-shrink: 0;
  transition: transform .2s;
  opacity: 0.7;
}

.dropdown-arrow.open {
  transform: rotate(180deg);
}

/* ── Dropdown 菜单 ── */
.nav-dropdown-menu {
  position: absolute;
  top: 100%;
  left: 0;
  min-width: 160px;
  background: #1e2a42;
  border: 1px solid rgba(255,255,255,.1);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0,0,0,.35);
  z-index: 200;
  padding: 4px 0;
  flex-direction: column;
  display: flex;  /* v-show 控制 visibility，不用 display:none/flex 切换 */
}

.nav-dropdown-menu button {
  background: transparent;
  border: none;
  color: rgba(255,255,255,.7);
  padding: 10px 16px;
  cursor: pointer;
  font-size: 13px;
  transition: all .15s;
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  width: 100%;
  text-align: left;
  border-radius: 0;
}

.nav-dropdown-menu button:hover {
  color: #fff;
  background: rgba(255,255,255,.08);
}

.nav-dropdown-menu button.active {
  color: #5470c6;
  background: rgba(84,112,198,.15);
  font-weight: 600;
}

/* ── User Bar ── */
.user-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
  margin-left: auto;
}

.user-name {
  display: flex;
  align-items: center;
  gap: 6px;
  color: rgba(255,255,255,.8);
  font-size: 13px;
  font-weight: 500;
}

.user-avatar {
  width: 16px;
  height: 16px;
  color: rgba(255,255,255,.6);
}

.logout-btn {
  background: rgba(255,255,255,.08);
  border: 1px solid rgba(255,255,255,.15);
  border-radius: 6px;
  padding: 5px 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  transition: all 0.15s;
}

.logout-btn:hover {
  background: rgba(255,255,255,.15);
  border-color: rgba(255,255,255,.25);
}

.logout-icon {
  width: 14px;
  height: 14px;
  color: rgba(255,255,255,.7);
}

/* ── Main ── */
main {
  padding: 16px;
}
main.iframe-mode {
  padding: 0;
}

/* ── Callback Module ── */
.callback-module {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.sub-tabs {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0;
  background: #fff;
  border-radius: 8px 8px 0 0;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  border-bottom: 2px solid #e9ecef;
  padding: 0 8px;
}

.sub-tab-buttons {
  display: flex;
  gap: 0;
}

.callback-user-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-right: 16px;
}

.callback-user-filter .filter-label {
  font-size: 13px;
  color: #666;
}

.callback-user-filter .user-select {
  padding: 4px 10px;
  border: 1px solid #ddd;
  border-radius: 4px;
  background: #fff;
  font-size: 13px;
  cursor: pointer;
  color: #333;
  outline: none;
}

.callback-user-filter .user-select:focus {
  border-color: #5470c6;
}

.sub-tabs button {
  background: transparent;
  border: none;
  color: #888;
  padding: 12px 20px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all .15s;
  display: flex;
  align-items: center;
  gap: 6px;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
}

.sub-tabs button:hover {
  color: #555;
  background: #f8f9fa;
}

.sub-tabs button.active {
  color: #5470c6;
  border-bottom-color: #5470c6;
}

.sub-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

.sub-content {
  background: #fff;
  border-radius: 0 0 8px 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  padding: 20px;
}

@media (max-width: 900px) {
  header {
    flex-direction: column;
    height: auto;
    padding: 12px 16px;
    gap: 12px;
  }
  nav {
    width: 100%;
    overflow-x: auto;
  }
  nav button {
    height: 40px;
    padding: 0 12px;
    font-size: 12px;
  }
  .user-bar {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
