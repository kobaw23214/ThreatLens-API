<script setup>
import { ref, onMounted } from 'vue'
import { authFetch, getFeatures } from '../utils/auth.js'
import CacheManagement from './CacheManagement.vue'

const emit = defineEmits(['open-graph'])

// 功能模块开关：控制功能相关设置子 tab 的显示
const features = getFeatures()

// ─── 用户管理 ────────────────────────────────────────────────────────────────
const users = ref([])
const userTotal = ref(0)
const userPage = ref(1)
const loadingUsers = ref(true)

// 创建用户对话框
const showCreateUser = ref(false)
const newUsername = ref('')
const newUserPwd = ref('')
const newLimit5h = ref(2000)
const newLimitWeekly = ref(100000)
const creatingUser = ref(false)

// 编辑用户对话框
const editingUser = ref(null)
const editLimit5h = ref(0)
const editLimitWeekly = ref(0)
const editIsActive = ref(true)
const editPassword = ref('')
const savingUser = ref(false)

// 查看用户 Key
const viewKeysUser = ref(null)
const viewKeys = ref([])
const loadingKeys = ref(false)

// 为用户创建 Key
const showCreateKeyForUser = ref(false)
const keyName = ref('')
const keyExpireDays = ref(30)
const creatingKey = ref(false)
const createdKeyResult = ref(null)
const copyTip = ref('')

// 使用统计
const usageStats = ref([])
const loadingStats = ref(true)

// 子 tab
const adminSubTab = ref('users')

// ─── 联网解析代理（TI）───────────────────────────────────────────────────────
const tiProxyLoading = ref(false)
const tiProxySaving  = ref(false)
const tiProxyMsg     = ref('')
const tiProxyErr     = ref('')
const tiProxyForm    = ref({ proxy_enabled: false, proxy_url: '', threatbook: '', qianxin: '' })
const tiProxyView    = ref(null)   // GET /ti/settings 掩码视图（含 effective_proxy）
const tiSessions     = ref([])     // GET /ti/sessions（含 proxy_used）
const tiProxyTesting = ref(false)
const tiProxyTestRes = ref([])

async function fetchTiProxy() {
  tiProxyLoading.value = true
  tiProxyErr.value = ''
  tiProxyMsg.value = ''
  try {
    const [sRes, sessRes] = await Promise.all([
      authFetch('/ti/settings'),
      authFetch('/ti/sessions'),
    ])
    if (sRes.status === 503) { tiProxyErr.value = 'TI 联网情报服务未启用'; return }
    if (sRes.ok) {
      const s = await sRes.json()
      tiProxyView.value = s
      // URL 字段不回填（掩码视图不含明文账密）：留空 = 保持不变
      tiProxyForm.value = {
        proxy_enabled: !!s.proxy_enabled,
        proxy_url: '',
        threatbook: '',
        qianxin: '',
      }
    } else {
      tiProxyErr.value = (await sRes.json().catch(() => ({}))).detail || '设置加载失败'
    }
    if (sessRes.ok) tiSessions.value = (await sessRes.json()).items || []
  } catch (e) {
    tiProxyErr.value = '加载失败: ' + e.message
  } finally {
    tiProxyLoading.value = false
  }
}

async function saveTiProxy() {
  tiProxySaving.value = true
  tiProxyMsg.value = ''
  tiProxyErr.value = ''
  try {
    // 留空 = 该字段保持不变（掩码视图拿不到明文，也不该回填）
    const body = { proxy_enabled: tiProxyForm.value.proxy_enabled }
    if (tiProxyForm.value.proxy_url.trim())  body.proxy_url = tiProxyForm.value.proxy_url.trim()
    if (tiProxyForm.value.threatbook.trim()) body.proxy_url_threatbook = tiProxyForm.value.threatbook.trim()
    if (tiProxyForm.value.qianxin.trim())    body.proxy_url_qianxin = tiProxyForm.value.qianxin.trim()
    const res = await authFetch('/ti/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) { tiProxyErr.value = data.detail || '保存失败'; return }
    tiProxyView.value = data.settings
    tiProxyMsg.value = '已保存，下一个任务起生效（浏览器自动重建，会话不丢）'
    fetchTiProxy()  // 清空输入框 + 刷新会话状态
  } catch (e) {
    tiProxyErr.value = '保存失败: ' + e.message
  } finally {
    tiProxySaving.value = false
  }
}

// 清除某级代理地址（立即生效，无需配合保存）
async function clearTiProxyField(key) {
  tiProxyErr.value = ''
  tiProxyMsg.value = ''
  try {
    const res = await authFetch('/ti/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: '' }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) { tiProxyErr.value = data.detail || '清除失败'; return }
    tiProxyView.value = data.settings
    tiProxyMsg.value = '已清除，下一个任务起生效'
    fetchTiProxy()
  } catch (e) {
    tiProxyErr.value = '清除失败: ' + e.message
  }
}

// 字段占位提示：显示当前已配置的脱敏地址
function tiProxyPlaceholder(key) {
  const v = key === 'proxy_url'
    ? tiProxyView.value?.proxy_url
    : tiProxyView.value?.platforms?.[key]?.proxy_url
  if (!v || !v.url) return '未配置'
  return `当前: ${v.url}${v.auth_set ? '（含认证）' : ''}，留空保持不变`
}

// 该级是否已配置地址（决定「清除」按钮显隐）
function tiProxyConfigured(key) {
  const v = key === 'proxy_url'
    ? tiProxyView.value?.proxy_url
    : tiProxyView.value?.platforms?.[key]?.proxy_url
  return !!(v && v.url)
}

async function testTiProxy() {
  tiProxyTesting.value = true
  tiProxyTestRes.value = []
  tiProxyErr.value = ''
  try {
    // 表单全局地址非空 → 测该（未保存的）地址；为空 → 按已生效配置逐平台测
    const res = await authFetch('/ti/settings/proxy-test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxy_url: tiProxyForm.value.proxy_url.trim() }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) { tiProxyErr.value = data.detail || '自检失败'; return }
    tiProxyTestRes.value = data.items || []
  } catch (e) {
    tiProxyErr.value = '自检失败: ' + e.message
  } finally {
    tiProxyTesting.value = false
  }
}

function tiSessionOf(platform) {
  return tiSessions.value.find(s => s.platform === platform) || {}
}

// ─── 路由配置 ────────────────────────────────────────────────────────────────────────────
const routingConfig = ref({ routes: [] })
const routingModels = ref([])  // 已加载的模型列表
const routingLoading = ref(false)
const routingSaving  = ref(false)
const routingSaveMsg = ref('')  // 成功提示
const routingSaveErr = ref('')  // 错误提示
const newRouteModel  = ref('')  // 新增路由的模型
const newCanonical   = ref({})  // {routeIndex: 新增 canonical 类型输入框}
const editAlias      = ref({})  // {`${routeIndex}::${canonical}`: 当前正在输入的别名}

async function fetchRouting() {
  routingLoading.value = true
  routingSaveMsg.value = ''
  routingSaveErr.value = ''
  try {
    const res = await authFetch('/admin/auto-routing')
    if (res.ok) {
      const data = await res.json()
      // 深拷贝避免引用问题
      routingConfig.value = JSON.parse(JSON.stringify(data.config))
      if (!Array.isArray(routingConfig.value.routes)) routingConfig.value.routes = []
      // 兼容旧版数组形态 → 统一成 {canonical: [别名,...]} 对象形态; priority 缺省补 0
      for (const r of routingConfig.value.routes) {
        r.labels = normalizeLabels(r.labels)
        if (typeof r.priority !== 'number') r.priority = 0
      }
      routingModels.value = data.models_info || []
    } else {
      const err = await res.json()
      routingSaveErr.value = err.detail || '加载失败'
    }
  } catch (e) {
    routingSaveErr.value = e.message
  } finally {
    routingLoading.value = false
  }
}

// 校验优先级: 0~100 整数且各 route 唯一。返回错误信息(无错误返回 '')。
function validatePriorities() {
  const seen = {}
  for (const r of routingConfig.value.routes) {
    const p = r.priority
    if (!Number.isInteger(p) || p < 0 || p > 100) {
      return `模型「${r.model}」的优先级必须是 0~100 的整数`
    }
    if (p in seen) {
      return `优先级 ${p} 重复：「${seen[p]}」与「${r.model}」不能相同`
    }
    seen[p] = r.model
  }
  return ''
}

async function saveRouting() {
  const prErr = validatePriorities()
  if (prErr) { routingSaveErr.value = prErr; return }
  routingSaving.value  = true
  routingSaveMsg.value = ''
  routingSaveErr.value = ''
  try {
    const payload = {
      routes: routingConfig.value.routes.map(r => ({
        model: r.model,
        priority: r.priority,
        labels: JSON.parse(JSON.stringify(r.labels)),
      })),
    }
    const res = await authFetch('/admin/auto-routing', { method: 'POST', body: payload })
    if (res.ok) {
      const data = await res.json()
      const fb = (data.fallback_models && data.fallback_models.length) ? data.fallback_models.join('、') : '(无)'
      routingSaveMsg.value = `已保存并热重载！兜底模型：${fb}`
      // 重新拉取以同步文件状态
      await fetchRouting()
    } else {
      const err = await res.json()
      routingSaveErr.value = err.detail || '保存失败'
    }
  } catch (e) {
    routingSaveErr.value = e.message
  } finally {
    routingSaving.value = false
  }
}

// 把 labels 统一成 {canonical: [别名,...]} 对象形态(兼容旧版数组)
function normalizeLabels(labels) {
  const obj = {}
  if (Array.isArray(labels)) {
    for (const lb of labels) if (typeof lb === 'string') obj[lb] = []
  } else if (labels && typeof labels === 'object') {
    for (const [k, v] of Object.entries(labels)) obj[k] = Array.isArray(v) ? [...v] : []
  }
  return obj
}

// 新增一个 canonical 实体类型(空别名)到某条路由
function addCanonicalToRoute(routeIndex) {
  const raw = (newCanonical.value[routeIndex] || '').trim()
  if (!raw) return
  const route = routingConfig.value.routes[routeIndex]
  if (!(raw in route.labels)) route.labels[raw] = []
  newCanonical.value[routeIndex] = ''
}

function removeCanonicalFromRoute(routeIndex, canonical) {
  delete routingConfig.value.routes[routeIndex].labels[canonical]
}

// 给某个 canonical 类型添加别名(支持逗号/顿号等分隔)
function addAliasToCanonical(routeIndex, canonical) {
  const key = routeIndex + '::' + canonical
  const raw = (editAlias.value[key] || '').trim()
  if (!raw) return
  const aliases = raw.split(/[,，、;; \t]+/).map(s => s.trim()).filter(Boolean)
  const arr = routingConfig.value.routes[routeIndex].labels[canonical]
  for (const a of aliases) {
    if (!arr.includes(a)) arr.push(a)
  }
  editAlias.value[key] = ''
}

function removeAliasFromCanonical(routeIndex, canonical, aliasIndex) {
  routingConfig.value.routes[routeIndex].labels[canonical].splice(aliasIndex, 1)
}

function removeRoute(routeIndex) {
  routingConfig.value.routes.splice(routeIndex, 1)
}

// 取一个未被占用的优先级(从 100 往下找)
function nextFreePriority() {
  const used = new Set(routingConfig.value.routes.map(r => r.priority))
  for (let p = 100; p >= 0; p--) {
    if (!used.has(p)) return p
  }
  return 0
}

function addNewRoute() {
  const model = newRouteModel.value.trim()
  if (!model) return
  const existing = routingConfig.value.routes.find(r => r.model === model)
  if (!existing) {
    routingConfig.value.routes.push({ model, priority: nextFreePriority(), labels: {} })
  }
  newRouteModel.value = ''
}

// ─── ATT&CK Auto 语言比例路由 ───────────────────────────────────────────────
const attackRoutingConfig = ref({ version: 1, routes: [], fallback_model: 'qwen3-attack-retriever' })
const attackRoutingLoading = ref(false)
const attackRoutingSaving = ref(false)
const attackRoutingSaveMsg = ref('')
const attackRoutingSaveErr = ref('')
const attackRoutingActive = ref(false)

function attackMetricTitle(metric) {
  return metric === 'english_ratio' ? '英文字符占比' : '中文字符占比'
}

function attackMetricDescription(route) {
  return route.metric === 'english_ratio'
    ? '达到阈值时选择 TRAM；适合高纯度英文威胁报告。'
    : '达到阈值时选择自研 Qwen3 Retriever；未命中任何条件时也由该模型兜底。'
}

async function fetchAttackRouting() {
  attackRoutingLoading.value = true
  attackRoutingSaveMsg.value = ''
  attackRoutingSaveErr.value = ''
  try {
    const res = await authFetch('/admin/attack-auto-routing')
    const data = await res.json()
    if (!res.ok) {
      attackRoutingSaveErr.value = data.detail || '加载失败'
      return
    }
    attackRoutingConfig.value = JSON.parse(JSON.stringify(data.config))
    for (const route of attackRoutingConfig.value.routes || []) {
      route.threshold_percent = Math.round(Number(route.threshold) * 100)
    }
    attackRoutingActive.value = Boolean(data.active)
  } catch (e) {
    attackRoutingSaveErr.value = e.message
  } finally {
    attackRoutingLoading.value = false
  }
}

async function saveAttackRouting() {
  for (const route of attackRoutingConfig.value.routes || []) {
    if (!Number.isInteger(route.threshold_percent) || route.threshold_percent < 0 || route.threshold_percent > 100) {
      attackRoutingSaveErr.value = `${attackMetricTitle(route.metric)}必须是 0～100 的整数百分比`
      return
    }
  }
  attackRoutingSaving.value = true
  attackRoutingSaveMsg.value = ''
  attackRoutingSaveErr.value = ''
  const payload = {
    version: attackRoutingConfig.value.version || 1,
    routes: attackRoutingConfig.value.routes.map(route => ({
      model: route.model,
      metric: route.metric,
      operator: 'gte',
      threshold: route.threshold_percent / 100,
    })),
    fallback_model: 'qwen3-attack-retriever',
  }
  try {
    const res = await authFetch('/admin/attack-auto-routing', { method: 'POST', body: payload })
    const data = await res.json()
    if (!res.ok) {
      attackRoutingSaveErr.value = data.detail || '保存失败'
      return
    }
    attackRoutingSaveMsg.value = `已保存并热重载；兜底模型：${data.fallback_model}`
    await fetchAttackRouting()
    attackRoutingSaveMsg.value = `已保存并热重载；兜底模型：${data.fallback_model}`
  } catch (e) {
    attackRoutingSaveErr.value = e.message
  } finally {
    attackRoutingSaving.value = false
  }
}

function showError(err, defaultMsg) {
  let errMsg = defaultMsg
  if (err && err.detail) {
    if (typeof err.detail === 'string') {
      errMsg = err.detail
    } else if (Array.isArray(err.detail)) {
      errMsg = err.detail.map(d => {
        const field = d.loc && d.loc.length > 1 ? `${d.loc[1]}: ` : ''
        return `${field}${d.msg || JSON.stringify(d)}`
      }).join('\n')
    } else if (typeof err.detail === 'object') {
      errMsg = err.detail.message || JSON.stringify(err.detail)
    }
  }
  alert(errMsg)
}

async function fetchUsers() {
  loadingUsers.value = true
  try {
    const res = await authFetch(`/admin/users?page=${userPage.value}&page_size=50`)
    if (res.ok) {
      const data = await res.json()
      users.value = data.items
      userTotal.value = data.total
    }
  } finally {
    loadingUsers.value = false
  }
}

async function createUser() {
  creatingUser.value = true
  try {
    const res = await authFetch('/admin/users', {
      method: 'POST',
      body: {
        username: newUsername.value.trim(),
        password: newUserPwd.value,
        limit_5h: newLimit5h.value,
        limit_weekly: newLimitWeekly.value,
      },
    })
    if (res.ok) {
      showCreateUser.value = false
      newUsername.value = ''
      newUserPwd.value = ''
      newLimit5h.value = 2000
      newLimitWeekly.value = 100000
      await fetchUsers()
    } else {
      const err = await res.json()
      showError(err, '创建失败')
    }
  } finally {
    creatingUser.value = false
  }
}

function openEdit(u) {
  editingUser.value = { ...u }
  editLimit5h.value = u.limit_5h
  editLimitWeekly.value = u.limit_weekly
  editIsActive.value = u.is_active
  editPassword.value = ''
}

async function saveUser() {
  savingUser.value = true
  try {
    const body = {
      limit_5h: editLimit5h.value,
      limit_weekly: editLimitWeekly.value,
      is_active: editIsActive.value,
    }
    if (editPassword.value) body.password = editPassword.value

    const res = await authFetch(`/admin/users/${editingUser.value.id}`, {
      method: 'PUT',
      body,
    })
    if (res.ok) {
      editingUser.value = null
      await fetchUsers()
    } else {
      const err = await res.json()
      showError(err, '保存失败')
    }
  } finally {
    savingUser.value = false
  }
}

async function deleteUser(userId) {
  if (!confirm('确定要删除此用户吗？关联的 API Key 和调用记录也会被删除。')) return
  const res = await authFetch(`/admin/users/${userId}`, { method: 'DELETE' })
  if (res.ok) {
    await fetchUsers()
  } else {
    const err = await res.json()
    showError(err, '删除失败')
  }
}

// ─── 查看/创建用户 Key ──────────────────────────────────────────────────────

async function viewUserKeys(u) {
  viewKeysUser.value = u
  loadingKeys.value = true
  try {
    const res = await authFetch(`/admin/users/${u.id}/api-keys`)
    if (res.ok) viewKeys.value = await res.json()
  } finally {
    loadingKeys.value = false
  }
}

async function createKeyForUser() {
  creatingKey.value = true
  createdKeyResult.value = null
  try {
    const res = await authFetch(`/admin/users/${viewKeysUser.value.id}/api-keys`, {
      method: 'POST',
      body: { name: keyName.value, expires_days: keyExpireDays.value },
    })
    if (res.ok) {
      createdKeyResult.value = await res.json()
      showCreateKeyForUser.value = false
      keyName.value = ''
      keyExpireDays.value = 30
      // 刷新
      await viewUserKeys(viewKeysUser.value)
    } else {
      const err = await res.json()
      showError(err, '创建失败')
    }
  } finally {
    creatingKey.value = false
  }
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    copyTip.value = text
    setTimeout(() => copyTip.value = '', 2000)
  })
}

// ─── 使用统计 ────────────────────────────────────────────────────────────────

async function fetchUsageStats() {
  loadingStats.value = true
  try {
    const res = await authFetch('/admin/usage-stats?page_size=100')
    if (res.ok) {
      const data = await res.json()
      usageStats.value = data.items
    }
  } finally {
    loadingStats.value = false
  }
}

function formatDate(d) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN')
}

function isExpired(d) {
  return d ? new Date(d) < new Date() : false
}

onMounted(() => {
  fetchUsers()
  fetchUsageStats()
})
</script>

<template>
  <div class="admin-panel">
    <!-- 子 Tab -->
    <div class="admin-sub-tabs">
      <button :class="{ active: adminSubTab === 'users' }" @click="adminSubTab = 'users'">
        <svg viewBox="0 0 18 18" class="tab-icon">
          <circle cx="9" cy="6" r="3.5" fill="none" stroke="currentColor" stroke-width="1.3"/>
          <path d="M2 17c0-4 3-6.5 7-6.5s7 2.5 7 6.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
        </svg>
        用户管理
      </button>
      <button :class="{ active: adminSubTab === 'stats' }" @click="adminSubTab = 'stats'; fetchUsageStats()">
        <svg viewBox="0 0 18 18" class="tab-icon">
          <rect x="2" y="10" width="3" height="6" rx="1" fill="currentColor" opacity=".8"/>
          <rect x="7.5" y="6" width="3" height="10" rx="1" fill="currentColor" opacity=".6"/>
          <rect x="13" y="3" width="3" height="13" rx="1" fill="currentColor" opacity=".4"/>
        </svg>
        使用统计
      </button>
      <button :class="{ active: adminSubTab === 'cache' }" @click="adminSubTab = 'cache'">
        <svg viewBox="0 0 18 18" class="tab-icon">
          <ellipse cx="9" cy="4.5" rx="6" ry="2.5" fill="none" stroke="currentColor" stroke-width="1.3"/>
          <path d="M3 4.5v9c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5v-9" fill="none" stroke="currentColor" stroke-width="1.3"/>
          <path d="M3 9c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5" fill="none" stroke="currentColor" stroke-width="1.3"/>
        </svg>
        缓存管理
      </button>
      <button v-if="features.ner" :class="{ active: adminSubTab === 'routing' }" @click="adminSubTab = 'routing'; fetchRouting()">
        <svg viewBox="0 0 18 18" class="tab-icon">
          <circle cx="3" cy="9" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/>
          <circle cx="15" cy="4" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/>
          <circle cx="15" cy="14" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/>
          <path d="M5 9h4l4-5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          <path d="M5 9h4l4 5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
        </svg>
        NER Auto模型路由
      </button>
      <button v-if="features.attack" :class="{ active: adminSubTab === 'attack-routing' }" @click="adminSubTab = 'attack-routing'; fetchAttackRouting()">
        <svg viewBox="0 0 18 18" class="tab-icon">
          <path d="M2 14L7 9l3 3 6-8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
          <circle cx="7" cy="9" r="1.6" fill="#fff" stroke="currentColor" stroke-width="1.2"/>
          <circle cx="10" cy="12" r="1.6" fill="#fff" stroke="currentColor" stroke-width="1.2"/>
        </svg>
        ATT&amp;CK Auto模型路由
      </button>
      <button v-if="features.ti" :class="{ active: adminSubTab === 'ti-proxy' }" @click="adminSubTab = 'ti-proxy'; fetchTiProxy()">
        <svg viewBox="0 0 18 18" class="tab-icon">
          <circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-width="1.3"/>
          <path d="M2 9h14" fill="none" stroke="currentColor" stroke-width="1.2"/>
          <path d="M9 2c2.8 2.2 2.8 11.8 0 14M9 2c-2.8 2.2-2.8 11.8 0 14" fill="none" stroke="currentColor" stroke-width="1.2"/>
        </svg>
        联网解析代理
      </button>
    </div>

    <!-- 用户管理 -->
    <div v-if="adminSubTab === 'users'" class="admin-content">
      <div class="toolbar">
        <span class="total-info">共 {{ userTotal }} 个用户</span>
        <button class="btn-primary" @click="showCreateUser = true">+ 创建用户</button>
      </div>

      <!-- 创建用户对话框 -->
      <div v-if="showCreateUser" class="dialog-card">
        <h4>创建新用户</h4>
        <div class="dialog-form">
          <div class="form-field">
            <label>用户名</label>
            <input v-model="newUsername" type="text" placeholder="2-50 字符" />
          </div>
          <div class="form-field">
            <label>密码</label>
            <input v-model="newUserPwd" type="password" placeholder="至少 6 位" />
          </div>
          <div class="form-row">
            <div class="form-field">
              <label>5小时限额</label>
              <input v-model.number="newLimit5h" type="number" min="0" />
            </div>
            <div class="form-field">
              <label>每周限额</label>
              <input v-model.number="newLimitWeekly" type="number" min="0" />
            </div>
          </div>
          <p class="hint">限额设为 0 表示无限制</p>
          <div class="form-actions">
            <button class="btn-primary" :disabled="creatingUser" @click="createUser">
              {{ creatingUser ? '创建中...' : '确认创建' }}
            </button>
            <button class="btn-text" @click="showCreateUser = false">取消</button>
          </div>
        </div>
      </div>

      <!-- 编辑用户对话框 -->
      <div v-if="editingUser" class="dialog-card">
        <h4>编辑用户：{{ editingUser.username }}</h4>
        <div class="dialog-form">
          <div class="form-row">
            <div class="form-field">
              <label>5小时限额</label>
              <input v-model.number="editLimit5h" type="number" min="0" />
            </div>
            <div class="form-field">
              <label>每周限额</label>
              <input v-model.number="editLimitWeekly" type="number" min="0" />
            </div>
          </div>
          <div class="form-field">
            <label>新密码（留空不修改）</label>
            <input v-model="editPassword" type="password" placeholder="不修改请留空" />
          </div>
          <div class="form-field">
            <label class="switch-label">
              <input type="checkbox" v-model="editIsActive" />
              <span>账户启用</span>
            </label>
          </div>
          <div class="form-actions">
            <button class="btn-primary" :disabled="savingUser" @click="saveUser">
              {{ savingUser ? '保存中...' : '保存' }}
            </button>
            <button class="btn-text" @click="editingUser = null">取消</button>
          </div>
        </div>
      </div>

      <!-- 查看用户 Key -->
      <div v-if="viewKeysUser" class="dialog-card">
        <div class="dialog-header">
          <h4>{{ viewKeysUser.username }} 的 API Key</h4>
          <button class="btn-text" @click="viewKeysUser = null; createdKeyResult = null">关闭</button>
        </div>

        <!-- Key 创建成功提示 -->
        <div v-if="createdKeyResult" class="key-created-alert">
          <div class="alert-header success">
            API Key 已创建！请复制保存。
          </div>
          <div class="key-display">
            <code>{{ createdKeyResult.key }}</code>
            <button class="btn-copy" @click="copyToClipboard(createdKeyResult.key)">
              {{ copyTip === createdKeyResult.key ? '✓ 已复制' : '复制' }}
            </button>
          </div>
        </div>

        <div class="toolbar" style="margin-top: 12px">
          <span></span>
          <button class="btn-primary btn-sm" @click="showCreateKeyForUser = true">+ 创建 Key</button>
        </div>

        <div v-if="showCreateKeyForUser" class="inline-form" style="margin-top: 12px">
          <div class="form-row">
            <div class="form-field">
              <label>名称</label>
              <input v-model="keyName" type="text" placeholder="用途描述" />
            </div>
            <div class="form-field" style="width: 140px">
              <label>过期天数</label>
              <input v-model.number="keyExpireDays" type="number" min="1" max="365" />
            </div>
          </div>
          <div class="form-actions">
            <button class="btn-primary btn-sm" :disabled="creatingKey" @click="createKeyForUser">
              {{ creatingKey ? '创建中...' : '确认' }}
            </button>
            <button class="btn-text" @click="showCreateKeyForUser = false">取消</button>
          </div>
        </div>

        <div v-if="loadingKeys" class="loading-text">加载中...</div>
        <table v-else-if="viewKeys.length" class="data-table" style="margin-top: 12px">
          <thead>
            <tr><th>前缀</th><th>名称</th><th>过期时间</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="k in viewKeys" :key="k.id">
              <td><code>{{ k.key_prefix }}...</code></td>
              <td>{{ k.name || '-' }}</td>
              <td :class="{ 'text-danger': isExpired(k.expires_at) }">{{ formatDate(k.expires_at) }}</td>
              <td>
                <span class="badge" :class="k.is_active && !isExpired(k.expires_at) ? 'badge-success' : 'badge-muted'">
                  {{ isExpired(k.expires_at) ? '已过期' : k.is_active ? '启用' : '停用' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-state small">暂无 API Key</div>
      </div>

      <!-- 用户列表 -->
      <div v-if="loadingUsers" class="loading-text">加载中...</div>
      <table v-else class="data-table users-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>用户名</th>
            <th>角色</th>
            <th>5h 限额</th>
            <th>周限额</th>
            <th>状态</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id" :class="{ disabled: !u.is_active }">
            <td>{{ u.id }}</td>
            <td>
              <strong>{{ u.username }}</strong>
            </td>
            <td>
              <span class="badge" :class="u.is_admin ? 'badge-admin' : 'badge-user'">
                {{ u.is_admin ? '管理员' : '用户' }}
              </span>
            </td>
            <td>{{ u.limit_5h === 0 ? '无限' : u.limit_5h }}</td>
            <td>{{ u.limit_weekly === 0 ? '无限' : u.limit_weekly }}</td>
            <td>
              <span class="badge" :class="u.is_active ? 'badge-success' : 'badge-danger'">
                {{ u.is_active ? '启用' : '禁用' }}
              </span>
            </td>
            <td>{{ formatDate(u.created_at) }}</td>
            <td class="actions-cell">
              <button class="btn-action" @click="openEdit(u)" title="编辑">
                <svg viewBox="0 0 16 16" class="action-icon"><path d="M11.5 2.5l2 2L5 13H3v-2l8.5-8.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
              </button>
              <button class="btn-action" @click="viewUserKeys(u)" title="API Key">
                <svg viewBox="0 0 16 16" class="action-icon"><path d="M10 2a3.5 3.5 0 00-3 5.3L2 12.3V15h2.7L9.7 10A3.5 3.5 0 0010 2z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><circle cx="10.5" cy="4.5" r="1" fill="currentColor"/></svg>
              </button>
              <button v-if="!u.is_admin" class="btn-action danger" @click="deleteUser(u.id)" title="删除">
                <svg viewBox="0 0 16 16" class="action-icon"><path d="M4 4h8l-.5 10H4.5L4 4zM6 2h4M2 4h12" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 使用统计 -->
    <div v-if="adminSubTab === 'stats'" class="admin-content">
      <div v-if="loadingStats" class="loading-text">加载中...</div>
      <table v-else class="data-table">
        <thead>
          <tr>
            <th>用户</th>
            <th>角色</th>
            <th>5h 已用 / 限额</th>
            <th>周已用 / 限额</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in usageStats" :key="s.id">
            <td><strong>{{ s.username }}</strong></td>
            <td>
              <span class="badge" :class="s.is_admin ? 'badge-admin' : 'badge-user'">
                {{ s.is_admin ? '管理员' : '用户' }}
              </span>
            </td>
            <td>
              <div class="usage-cell">
                <span>{{ s.used_5h }} / {{ s.limit_5h === 0 ? '∞' : s.limit_5h }}</span>
                <div v-if="s.limit_5h > 0" class="mini-bar">
                  <div class="mini-fill" :style="{ width: Math.min(100, s.used_5h / s.limit_5h * 100) + '%' }"
                    :class="{ warn: s.used_5h / s.limit_5h > 0.8 }"></div>
                </div>
              </div>
            </td>
            <td>
              <div class="usage-cell">
                <span>{{ s.used_weekly }} / {{ s.limit_weekly === 0 ? '∞' : s.limit_weekly }}</span>
                <div v-if="s.limit_weekly > 0" class="mini-bar">
                  <div class="mini-fill" :style="{ width: Math.min(100, s.used_weekly / s.limit_weekly * 100) + '%' }"
                    :class="{ warn: s.used_weekly / s.limit_weekly > 0.8 }"></div>
                </div>
              </div>
            </td>
            <td>
              <span class="badge" :class="s.is_active ? 'badge-success' : 'badge-danger'">
                {{ s.is_active ? '启用' : '禁用' }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 缓存管理 -->
    <div v-if="adminSubTab === 'cache'" class="admin-content">
      <CacheManagement @open-graph="emit('open-graph', $event)" />
    </div>

    <!-- 路由配置 -->
    <div v-if="adminSubTab === 'routing' && features.ner" class="admin-content">
      <div v-if="routingLoading" class="loading-text">加载中...</div>
      <template v-else>

        <!-- 页面头部 -->
        <div class="routing-header">
          <div>
            <h3 class="routing-title">Auto 模型路由配置</h3>
            <p class="routing-desc">
              每个模型配「优先级(0~100，越大越优先，各模型唯一)」+「实体类型(canonical)→别名」。左侧是引擎原始 tag（如 ipv4s / domains），右侧是用户可输入的中英文别名；无论输入类型名还是任一别名都会归一化映射到该类型（大小写不敏感，忽略空格/下划线/连字符）。同一类型可出现在多个模型中，结果按优先级合并：区间重叠时高优先级胜出。labels 留空的模型即「兜底模型」，处理所有未匹配的标签（如人物/组织等语义标签）。保存后即时生效，无需重启。
            </p>
          </div>
          <div class="routing-actions">
            <button class="btn-text" @click="fetchRouting" :disabled="routingLoading">↓ 重新加载</button>
            <button class="btn-primary" @click="saveRouting" :disabled="routingSaving">
              {{ routingSaving ? '保存中...' : '保存并应用' }}
            </button>
          </div>
        </div>

        <!-- 提示条 -->
        <div v-if="routingSaveMsg" class="routing-toast success">{{ routingSaveMsg }}</div>
        <div v-if="routingSaveErr" class="routing-toast error">⚠️ {{ routingSaveErr }}</div>

        <!-- 各模型路由列表 -->
        <div class="routing-section">
          <div class="routing-section-title">
            <span class="section-icon">🔀</span>
            标签路由规则
            <span class="section-hint">（对应模型处理指定标签）</span>
          </div>

          <div
            v-for="(route, ri) in routingConfig.routes"
            :key="ri"
            class="route-card"
          >
            <div class="route-card-header">
              <span class="route-model-name">{{ route.model }}</span>
              <span class="route-model-type">
                {{ (routingModels.find(m => m.name === route.model) || {}).type || '' }}
              </span>
              <span class="route-priority">
                优先级
                <input
                  v-model.number="route.priority"
                  type="number"
                  min="0"
                  max="100"
                  class="priority-input"
                  title="0~100，越大越优先，各模型唯一"
                />
              </span>
              <span v-if="!Object.keys(route.labels).length" class="route-fallback-badge" title="labels 为空，处理所有未匹配的标签">兜底</span>
              <button class="btn-remove-route" @click="removeRoute(ri)" title="删除此路由">✕</button>
            </div>

            <!-- canonical 类型 + 别名 编辑 -->
            <div class="route-canonical-groups">
              <div
                v-for="(aliases, canonical) in route.labels"
                :key="canonical"
                class="canonical-group"
              >
                <div class="canonical-head">
                  <span class="canonical-name" :title="canonical">{{ canonical }}</span>
                  <button
                    class="btn-remove-canonical"
                    @click="removeCanonicalFromRoute(ri, canonical)"
                    title="删除此类型"
                  >✕</button>
                </div>

                <div class="route-labels">
                  <span
                    v-for="(al, ai) in aliases"
                    :key="ai"
                    class="label-chip"
                  >
                    {{ al }}
                    <button @click="removeAliasFromCanonical(ri, canonical, ai)" class="chip-remove">×</button>
                  </span>
                  <span v-if="!aliases.length" class="empty-labels">无别名（仅按类型名匹配）</span>
                </div>

                <div class="route-add-label">
                  <input
                    v-model="editAlias[ri + '::' + canonical]"
                    type="text"
                    placeholder="添加别名，多个用逗号分隔"
                    @keyup.enter="addAliasToCanonical(ri, canonical)"
                    class="label-input"
                  />
                  <button class="btn-add-label" @click="addAliasToCanonical(ri, canonical)">+ 别名</button>
                </div>
              </div>

              <div v-if="!Object.keys(route.labels).length" class="empty-labels">暂无实体类型</div>
            </div>

            <!-- 新增 canonical 类型 -->
            <div class="route-add-label add-canonical">
              <input
                v-model="newCanonical[ri]"
                type="text"
                placeholder="新增实体类型（canonical，如 ipv4s / domains / cves）"
                @keyup.enter="addCanonicalToRoute(ri)"
                class="label-input"
              />
              <button class="btn-add-label" @click="addCanonicalToRoute(ri)">+ 类型</button>
            </div>
          </div>

          <!-- 无路由时的空状态 -->
          <div v-if="!routingConfig.routes.length" class="routing-empty">
            暂无路由规则，所有标签将由默认模型处理
          </div>
        </div>

        <!-- 新增路由行 -->
        <div class="routing-section">
          <div class="routing-section-title">
            <span class="section-icon">➕</span>
            新增路由
          </div>
          <div class="new-route-row">
            <select v-model="newRouteModel" class="model-select">
              <option value="">— 选择模型 —</option>
              <option
                v-for="m in routingModels.filter(m => ['ner', 'ioc', 'llm'].includes(m.type))"
                :key="m.name"
                :value="m.name"
              >{{ m.name }}</option>
            </select>
            <button
              class="btn-primary"
              :disabled="!newRouteModel"
              @click="addNewRoute"
            >新增路由</button>
          </div>
        </div>

        <!-- 底部保存按鈕 -->
        <div class="routing-footer">
          <button class="btn-primary btn-lg" @click="saveRouting" :disabled="routingSaving">
            {{ routingSaving ? '保存中...' : '💾 保存并即时生效' }}
          </button>
          <button class="btn-text" @click="fetchRouting">放弃更改</button>
        </div>

      </template>
    </div>

    <!-- ATT&CK Auto 语言比例路由 -->
    <div v-if="adminSubTab === 'attack-routing' && features.attack" class="admin-content">
      <div v-if="attackRoutingLoading" class="loading-text">加载中...</div>
      <template v-else>
        <div class="routing-header">
          <div>
            <h3 class="routing-title">ATT&amp;CK Auto 模型路由</h3>
            <p class="routing-desc">
              只统计 ASCII 英文字母和中文汉字，忽略数字、标点、URL、哈希和空白。按顺序判断：英文达到阈值选择 TRAM；否则中文达到阈值选择自研 Qwen3 Retriever；其余文本也由自研模型兜底。保存后立即热更新。
            </p>
          </div>
          <div class="routing-actions">
            <button class="btn-text" @click="fetchAttackRouting" :disabled="attackRoutingLoading">↓ 重新加载</button>
            <button class="btn-primary" @click="saveAttackRouting" :disabled="attackRoutingSaving">
              {{ attackRoutingSaving ? '保存中...' : '保存并应用' }}
            </button>
          </div>
        </div>

        <div v-if="!attackRoutingActive" class="routing-toast error">
          ⚠️ attack-auto 当前未加载，请确认 api/.env 的 ATTACK_MODELS_ENABLED 包含 auto，并重启 API。
        </div>
        <div v-if="attackRoutingSaveMsg" class="routing-toast success">{{ attackRoutingSaveMsg }}</div>
        <div v-if="attackRoutingSaveErr" class="routing-toast error">⚠️ {{ attackRoutingSaveErr }}</div>

        <div class="routing-section">
          <div class="routing-section-title">
            <span class="section-icon">🌐</span>
            语言占比条件
            <span class="section-hint">（分母仅为英文字符数 + 中文字符数）</span>
          </div>
          <div v-for="route in attackRoutingConfig.routes" :key="route.metric" class="attack-route-card">
            <div class="attack-route-head">
              <div>
                <div class="route-model-name">{{ route.model }}</div>
                <div class="attack-route-metric">{{ attackMetricTitle(route.metric) }}</div>
              </div>
              <strong class="attack-threshold-value">{{ route.threshold_percent }}%</strong>
            </div>
            <input
              v-model.number="route.threshold_percent"
              class="attack-threshold-slider"
              type="range"
              min="0"
              max="100"
              step="1"
            />
            <div class="attack-slider-scale"><span>0%</span><span>50%</span><span>100%</span></div>
            <p class="attack-route-help">{{ attackMetricDescription(route) }}</p>
          </div>
        </div>

        <div class="routing-section attack-fallback-row">
          <div>
            <div class="routing-section-title"><span class="section-icon">🛟</span>兜底模型</div>
            <p class="routing-desc">英文和中文条件都未命中，或文本不包含可判定语言字符时使用。</p>
          </div>
          <span class="route-fallback-badge">{{ attackRoutingConfig.fallback_model }}</span>
        </div>

        <div class="routing-footer">
          <button class="btn-primary btn-lg" @click="saveAttackRouting" :disabled="attackRoutingSaving || !attackRoutingActive">
            {{ attackRoutingSaving ? '保存中...' : '💾 保存并即时生效' }}
          </button>
          <button class="btn-text" @click="fetchAttackRouting">放弃更改</button>
        </div>
      </template>
    </div>

    <!-- 联网解析代理（TI 情报采集出网代理） -->
    <div v-if="adminSubTab === 'ti-proxy' && features.ti" class="admin-content">
      <div v-if="tiProxyLoading" class="loading-text">加载中...</div>
      <div v-else class="proxy-card">
        <h3 class="routing-title">联网解析代理</h3>
        <p class="proxy-hint">
          内网部署时，微步在线 / 奇安信的浏览器采集与人工登录（调用台「去登录」）均经出口代理出网。
          默认关闭（直连）；平台级覆盖优先于全局；保存后下一个任务生效，浏览器自动重建、会话不丢。格式：
          <code>http://user:pass@host:port</code> 或 <code>socks5://host:port</code>
          （需账密认证请用 http——Chromium 的 SOCKS5 不支持认证）。地址字段留空保存 = 保持不变；
          含账密的地址保存后不再回显明文，点「清除」可移除该级配置。
        </p>
        <div class="proxy-form">
          <label class="proxy-toggle">
            <input type="checkbox" v-model="tiProxyForm.proxy_enabled" />
            启用代理（关闭时地址保留不丢，再开即恢复）
          </label>
          <div class="proxy-field">
            <label>全局代理地址</label>
            <div class="proxy-input-row">
              <input v-model="tiProxyForm.proxy_url" :placeholder="tiProxyPlaceholder('proxy_url')" />
              <button v-if="tiProxyConfigured('proxy_url')" class="btn-text"
                      @click="clearTiProxyField('proxy_url')">清除</button>
            </div>
          </div>
          <div class="proxy-field">
            <label>微步在线覆盖（可空，空 = 跟随全局）</label>
            <div class="proxy-input-row">
              <input v-model="tiProxyForm.threatbook" :placeholder="tiProxyPlaceholder('threatbook')" />
              <button v-if="tiProxyConfigured('threatbook')" class="btn-text"
                      @click="clearTiProxyField('proxy_url_threatbook')">清除</button>
            </div>
          </div>
          <div class="proxy-field">
            <label>奇安信覆盖（可空，空 = 跟随全局）</label>
            <div class="proxy-input-row">
              <input v-model="tiProxyForm.qianxin" :placeholder="tiProxyPlaceholder('qianxin')" />
              <button v-if="tiProxyConfigured('qianxin')" class="btn-text"
                      @click="clearTiProxyField('proxy_url_qianxin')">清除</button>
            </div>
          </div>
          <div class="proxy-actions">
            <button class="btn-primary" :disabled="tiProxySaving" @click="saveTiProxy">
              {{ tiProxySaving ? '保存中…' : '保存' }}
            </button>
            <button class="btn-secondary" :disabled="tiProxyTesting" @click="testTiProxy">
              {{ tiProxyTesting ? '自检中…' : '连通性自检' }}
            </button>
            <span class="proxy-note">自检不起浏览器：全局地址非空测该地址，为空按已生效配置逐平台测</span>
            <span v-if="tiProxyMsg" class="proxy-ok">{{ tiProxyMsg }}</span>
            <span v-if="tiProxyErr" class="proxy-err">{{ tiProxyErr }}</span>
          </div>
        </div>

        <!-- 自检结果 -->
        <table v-if="tiProxyTestRes.length" class="data-table proxy-table">
          <thead>
            <tr><th>平台</th><th>经由</th><th>HTTP</th><th>耗时</th><th>错误</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in tiProxyTestRes" :key="r.platform">
              <td>{{ r.name }}</td>
              <td><code>{{ r.proxy }}</code></td>
              <td>{{ r.http_status ?? '—' }}</td>
              <td>{{ r.latency_ms }} ms</td>
              <td class="proxy-err">{{ r.error || '' }}</td>
            </tr>
          </tbody>
        </table>

        <!-- 当前生效状态 -->
        <h4 class="proxy-subtitle">当前生效状态</h4>
        <table class="data-table proxy-table">
          <thead>
            <tr><th>平台</th><th>生效代理</th><th>会话状态</th><th>当前会话经由</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in ['threatbook', 'qianxin']" :key="p">
              <td>{{ tiProxyView?.platforms?.[p]?.name || p }}</td>
              <td><code>{{ tiProxyView?.platforms?.[p]?.effective_proxy || '直连' }}</code></td>
              <td>{{ tiSessionOf(p).login_state || '—' }}</td>
              <td><code>{{ tiSessionOf(p).proxy_used || '直连' }}</code></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.admin-panel {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.admin-sub-tabs {
  display: flex;
  gap: 0;
  background: #fff;
  border-radius: 10px 10px 0 0;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
  border-bottom: 2px solid #e9ecef;
  padding: 0 8px;
}

.admin-sub-tabs button {
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

.admin-sub-tabs button:hover { color: #555; }
.admin-sub-tabs button.active {
  color: #5470c6;
  border-bottom-color: #5470c6;
}

.tab-icon { width: 16px; height: 16px; }

.admin-content {
  background: #fff;
  border-radius: 0 0 10px 10px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
  padding: 20px;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.total-info {
  font-size: 13px;
  color: #888;
}

/* 对话框 */
.dialog-card {
  background: #f8f9fc;
  border: 1px solid #e8ecf4;
  border-radius: 10px;
  padding: 20px;
  margin-bottom: 16px;
}

.dialog-card h4 {
  font-size: 15px;
  color: #1a1a2e;
  margin-bottom: 16px;
}

.dialog-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.dialog-header h4 { margin-bottom: 0; }

.dialog-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-row {
  display: flex;
  gap: 12px;
}

.form-field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
}

.form-field label {
  font-size: 12px;
  color: #666;
  font-weight: 500;
}

.form-field input[type="text"],
.form-field input[type="password"],
.form-field input[type="number"],
.form-field input:not([type]) {
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  transition: border-color 0.2s;
}

.form-field input:focus {
  border-color: #5470c6;
  box-shadow: 0 0 0 2px rgba(84,112,198,0.15);
}

.switch-label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.switch-label input[type="checkbox"] {
  width: 16px;
  height: 16px;
  accent-color: #5470c6;
}

.hint {
  font-size: 12px;
  color: #aaa;
  margin-bottom: 8px;
}

.form-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 表格 */
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.data-table th {
  text-align: left;
  padding: 10px 12px;
  background: #f8f9fc;
  color: #666;
  font-weight: 500;
  border-bottom: 1px solid #e8ecf4;
}

.data-table td {
  padding: 10px 12px;
  border-bottom: 1px solid #f0f2f5;
  color: #333;
}

.data-table tr.disabled td {
  opacity: 0.5;
}

.data-table code {
  background: #f0f2f5;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}

.actions-cell {
  display: flex;
  gap: 4px;
}

.btn-action {
  background: #f0f2f5;
  border: none;
  border-radius: 6px;
  padding: 5px;
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
}

.btn-action:hover {
  background: #e0e3e8;
}

.btn-action.danger:hover {
  background: #fee2e2;
}

.action-icon {
  width: 14px;
  height: 14px;
  color: #555;
}

.btn-action.danger .action-icon {
  color: #dc2626;
}

/* Badge */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}

.badge-success { background: #dcfce7; color: #15803d; }
.badge-danger  { background: #fee2e2; color: #dc2626; }
.badge-muted   { background: #f0f0f0; color: #888; }
.badge-admin   { background: #ede9fe; color: #7c3aed; }
.badge-user    { background: #e0f2fe; color: #0369a1; }

.text-danger { color: #dc2626; }

/* Usage cell */
.usage-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mini-bar {
  width: 80px;
  height: 4px;
  background: #e5e7eb;
  border-radius: 2px;
  overflow: hidden;
}

.mini-fill {
  height: 100%;
  background: #5470c6;
  border-radius: 2px;
  transition: width 0.3s;
}

.mini-fill.warn {
  background: #f59e0b;
}

/* Key 创建成功 */
.key-created-alert {
  background: #f0fdf4;
  border: 1px solid #86efac;
  border-radius: 10px;
  padding: 14px;
  margin-top: 12px;
}

.alert-header {
  font-size: 13px;
  font-weight: 500;
  color: #15803d;
  margin-bottom: 8px;
}

.key-display {
  display: flex;
  align-items: center;
  gap: 8px;
  background: #fff;
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid #d1fae5;
}

.key-display code {
  flex: 1;
  font-size: 12px;
  word-break: break-all;
  color: #333;
  background: transparent;
}

.inline-form {
  background: #fff;
  border: 1px solid #e8ecf4;
  border-radius: 8px;
  padding: 14px;
}

/* Buttons */
.btn-primary {
  background: linear-gradient(135deg, #5470c6, #3ba272);
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-primary:hover:not(:disabled) { opacity: 0.9; }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }

.btn-sm { padding: 6px 14px; font-size: 12px; }

.btn-text {
  background: transparent;
  border: none;
  color: #888;
  font-size: 13px;
  cursor: pointer;
  padding: 4px 8px;
}

.btn-text:hover { color: #555; }

.btn-copy {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.loading-text {
  text-align: center;
  padding: 32px;
  color: #888;
  font-size: 14px;
}

.empty-state {
  text-align: center;
  padding: 24px;
  color: #aaa;
  font-size: 13px;
}

.empty-state.small {
  padding: 16px;
}

@media (max-width: 900px) {
  .users-table { font-size: 12px; }
  .form-row { flex-direction: column; }
}

/* ─── 路由配置 Tab ─── */
.routing-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 20px;
  gap: 16px;
  flex-wrap: wrap;
}
.routing-title {
  font-size: 16px;
  font-weight: 700;
  color: #1a1a2e;
  margin: 0 0 4px;
}
.routing-desc {
  font-size: 12px;
  color: #888;
  margin: 0;
  max-width: 580px;
}
.routing-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}

/* 提示条 */
.routing-toast {
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
  margin-bottom: 16px;
}
.routing-toast.success {
  background: #f0fdf4;
  border: 1px solid #86efac;
  color: #15803d;
}
.routing-toast.error {
  background: #fef2f2;
  border: 1px solid #fca5a5;
  color: #b91c1c;
}

/* 分区块 */
.routing-section {
  background: #f8f9fc;
  border: 1px solid #e8ecf4;
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 14px;
}
.routing-section-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.section-icon { font-size: 14px; }
.section-hint {
  font-weight: 400;
  color: #9ca3af;
  font-size: 12px;
}

/* 默认模型行 */
.default-model-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.model-select {
  padding: 7px 10px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 13px;
  background: #fff;
  color: #333;
  outline: none;
  min-width: 200px;
  cursor: pointer;
  transition: border-color 0.2s;
}
.model-select:focus { border-color: #5470c6; }
.default-model-badge {
  font-size: 12px;
  color: #6b7280;
  background: #e0e7ff;
  padding: 3px 10px;
  border-radius: 10px;
}

/* 路由卡片 */
.route-card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 10px;
  transition: box-shadow 0.15s;
}
.route-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.07); }

.route-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.route-model-name {
  font-size: 14px;
  font-weight: 700;
  color: #1a1a2e;
}
.route-model-type {
  font-size: 11px;
  color: #888;
  background: #f0f2f5;
  padding: 1px 7px;
  border-radius: 8px;
}
.route-priority {
  font-size: 11px;
  color: #555;
  display: flex;
  align-items: center;
  gap: 4px;
}
.priority-input {
  width: 54px;
  font-size: 12px;
  padding: 2px 6px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  text-align: center;
}
.route-fallback-badge {
  font-size: 11px;
  color: #b45309;
  background: #fef3c7;
  padding: 1px 8px;
  border-radius: 8px;
  font-weight: 600;
}
.btn-remove-route {
  margin-left: auto;
  background: none;
  border: 1px solid #e5e7eb;
  color: #9ca3af;
  border-radius: 6px;
  width: 26px;
  height: 26px;
  cursor: pointer;
  font-size: 12px;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
}
.btn-remove-route:hover {
  background: #fee2e2;
  border-color: #fca5a5;
  color: #dc2626;
}

/* 标签 chip */
.route-labels {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
  min-height: 28px;
}
.label-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px 3px 10px;
  background: #eef2ff;
  color: #3730a3;
  border: 1px solid #c7d2fe;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 500;
}
.chip-remove {
  background: none;
  border: none;
  color: #818cf8;
  cursor: pointer;
  font-size: 13px;
  padding: 0;
  line-height: 1;
  transition: color 0.15s;
}
.chip-remove:hover { color: #dc2626; }
.empty-labels {
  font-size: 12px;
  color: #bbb;
  align-self: center;
}

/* 新增标签输入 */
.route-add-label {
  display: flex;
  gap: 6px;
  align-items: center;
}
.label-input {
  flex: 1;
  padding: 6px 10px;
  border: 1px solid #d1d5db;
  border-radius: 7px;
  font-size: 12px;
  outline: none;
  transition: border-color 0.2s;
}
.label-input:focus { border-color: #5470c6; }
.btn-add-label {
  padding: 6px 14px;
  background: #eef2ff;
  color: #3730a3;
  border: 1px solid #c7d2fe;
  border-radius: 7px;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s;
}
.btn-add-label:hover { background: #e0e7ff; }

/* canonical 类型分组 */
.route-canonical-groups {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 10px;
}
.canonical-group {
  border: 1px solid #eef0f4;
  border-radius: 8px;
  padding: 8px 10px;
  background: #fafbfc;
}
.canonical-head {
  display: flex;
  align-items: center;
  margin-bottom: 6px;
}
.canonical-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  font-weight: 600;
  color: #0f766e;
  background: #ccfbf1;
  border: 1px solid #99f6e4;
  border-radius: 6px;
  padding: 2px 8px;
}
.btn-remove-canonical {
  margin-left: auto;
  background: none;
  border: 1px solid #e5e7eb;
  color: #9ca3af;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.btn-remove-canonical:hover { background: #fee2e2; border-color: #fca5a5; color: #dc2626; }
.add-canonical { margin-top: 4px; }

/* 新增路由行 */
.new-route-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

/* 空状态 */
.routing-empty {
  text-align: center;
  padding: 20px;
  color: #bbb;
  font-size: 13px;
}

/* 底部 */
.routing-footer {
  display: flex;
  gap: 10px;
  align-items: center;
  padding-top: 8px;
}
.btn-lg {
  padding: 10px 24px;
  font-size: 14px;
}

/* ATT&CK Auto 语言比例滑动条 */
.attack-route-card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 12px;
}
.attack-route-card:last-child { margin-bottom: 0; }
.attack-route-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.attack-route-metric {
  color: #6b7280;
  font-size: 12px;
  margin-top: 3px;
}
.attack-threshold-value {
  color: #5470c6;
  font-size: 22px;
  min-width: 64px;
  text-align: right;
}
.attack-threshold-slider {
  width: 100%;
  accent-color: #5470c6;
  cursor: pointer;
}
.attack-slider-scale {
  display: flex;
  justify-content: space-between;
  color: #9ca3af;
  font-size: 10px;
  margin-top: 2px;
}
.attack-route-help {
  color: #6b7280;
  font-size: 12px;
  margin: 10px 0 0;
}
.attack-fallback-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.attack-fallback-row .routing-section-title { margin-bottom: 4px; }

@media (max-width: 700px) {
  .admin-sub-tabs { overflow-x: auto; }
  .admin-sub-tabs button { white-space: nowrap; padding: 12px 14px; }
  .attack-fallback-row { align-items: flex-start; flex-direction: column; }
}

/* ── 联网解析代理 ── */
.proxy-card {
  background: #fff;
  border-radius: 8px;
  padding: 20px 24px;
}
.proxy-hint {
  font-size: 13px;
  color: #777;
  line-height: 1.7;
  margin: 4px 0 16px;
}
.proxy-hint code { background: #f5f5f5; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.proxy-form { display: flex; flex-direction: column; gap: 12px; max-width: 560px; }
.proxy-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: #333;
  cursor: pointer;
}
.proxy-field { display: flex; flex-direction: column; gap: 4px; }
.proxy-field label { font-size: 13px; color: #666; }
.proxy-input-row { display: flex; align-items: center; gap: 8px; }
.proxy-input-row input {
  flex: 1;
  padding: 8px 12px;
  border: 1px solid #ddd;
  border-radius: 6px;
  font-size: 13px;
  font-family: monospace;
  outline: none;
}
.proxy-input-row input:focus { border-color: #667eea; }
.proxy-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 4px; }
.btn-secondary {
  padding: 8px 16px;
  border: 1px solid #667eea;
  border-radius: 6px;
  background: #fff;
  color: #667eea;
  font-size: 13px;
  cursor: pointer;
}
.btn-secondary:hover:not(:disabled) { background: #f0f2ff; }
.btn-secondary:disabled { opacity: 0.6; cursor: not-allowed; }
.proxy-note { font-size: 12px; color: #aaa; }
.proxy-ok { font-size: 13px; color: #27ae60; }
.proxy-err { font-size: 13px; color: #e74c3c; }
.proxy-subtitle { font-size: 14px; color: #333; margin: 20px 0 8px; }
.proxy-table td code { font-size: 12px; }
</style>
