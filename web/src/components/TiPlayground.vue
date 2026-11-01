<script setup>
/**
 * 联网情报调用台
 * 与 NER / ATT&CK 调用台同构：参数配置 + curl 预览 + 两步执行。
 *   Step 1: POST /ti/expand     → 每个目标平台一个 task_id（202）
 *   Step 2: GET  /ti/tasks/{id} → 轮询查询平台原生区块（sections）
 * 采集说明：后端以浏览器登录微步在线 / 奇安信真实页面取数，
 * 同平台串行 + 随机间隔（反爬纪律），任务耗时通常 10~40 秒。
 */
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { authFetch } from '../utils/auth.js'
import { highlightCurl, highlightJson } from '../utils/highlight.js'

const emit = defineEmits(['open-graph'])

// ─── 平台 / 类型元数据 ─────────────────────────────────────────────────────
const PLATFORM_LABELS = { threatbook: '微步在线', qianxin: '奇安信' }
const SEED_TYPES = [
  { value: 'ip',     label: 'IP 地址',  placeholder: '如 8.8.8.8' },
  { value: 'domain', label: '域名',     placeholder: '如 example.com' },
  { value: 'file',   label: '样本哈希', placeholder: 'MD5 / SHA1 / SHA256' },
]
const SESSION_STATES = {
  ok:           { label: '会话有效',   cls: 'ok' },
  logging_in:   { label: '登录中',     cls: 'warn' },
  need_manual:  { label: '需人工登录', cls: 'bad' },
  auth_failed:  { label: '凭据失效',   cls: 'bad' },
  expired:      { label: '会话过期',   cls: 'bad' },
  unknown:      { label: '未查询',     cls: '' },
}

// ─── 请求参数 ──────────────────────────────────────────────────────────────
const apiBase    = ref(location.origin)
const seedType   = ref('ip')
const seedValue  = ref('')
const platforms  = ref({ threatbook: true, qianxin: true })
const reuseCache = ref(true)

// 回调配置（可选，与 NER 调用台同构）
const callbackEnabled = ref(false)
const callbackUrl     = ref('')
const callbackMethod  = ref('POST')
const callbackTimeout = ref(30)
const callbackHeaders = ref({})
const callbackHeadersEnabled = ref(false)  // 自定义请求头开关，默认关闭
// JSON 编辑器的字符串表示与校验（与 NER/ATT&CK 调用台一致）
const callbackHeadersStr   = ref(JSON.stringify(callbackHeaders.value, null, 2))
const callbackHeadersError = ref('')

function formatCallbackHeaders() {
  try {
    const parsed = JSON.parse(callbackHeadersStr.value)
    callbackHeadersStr.value = JSON.stringify(parsed, null, 2)
    callbackHeadersError.value = ''
  } catch (e) {
    callbackHeadersError.value = 'JSON 格式错误，无法格式化: ' + e.message
  }
}

watch(callbackHeadersStr, (newVal) => {
  try {
    const parsed = JSON.parse(newVal)
    if (typeof parsed === 'object' && !Array.isArray(parsed)) {
      callbackHeaders.value = parsed
      callbackHeadersError.value = ''
    } else {
      callbackHeadersError.value = 'headers 必须是对象格式'
    }
  } catch (e) {
    callbackHeadersError.value = 'JSON 格式错误: ' + e.message
  }
})

// information_json 消息绑定（与 NER/ATT&CK 调用台一致：随结果/回调原样返回）
const informationJson  = ref({})
const informationStr   = ref(JSON.stringify(informationJson.value, null, 2))
const informationError = ref('')

function formatInformation() {
  try {
    const parsed = JSON.parse(informationStr.value)
    informationStr.value = JSON.stringify(parsed, null, 2)
    informationError.value = ''
  } catch (e) {
    informationError.value = 'JSON 格式错误，无法格式化: ' + e.message
  }
}

watch(informationStr, (newVal) => {
  try {
    const parsed = JSON.parse(newVal)
    if (typeof parsed === 'object' && !Array.isArray(parsed)) {
      informationJson.value = parsed
      informationError.value = ''
    } else {
      informationError.value = 'information_json 必须是对象格式'
    }
  } catch (e) {
    informationError.value = 'JSON 格式错误: ' + e.message
  }
})
const backendBase     = ref('')
const defaultCallbackUrl = computed(() =>
  backendBase.value ? `${backendBase.value}/callback/default` : ''
)

const sessions = ref([])  // /ti/sessions 红绿灯

function seedPlaceholder() {
  return (SEED_TYPES.find(t => t.value === seedType.value) || {}).placeholder || ''
}

// ─── curl 预览 ──────────────────────────────────────────────────────────────
const step1Body = computed(() => {
  const plats = Object.keys(platforms.value).filter(p => platforms.value[p])
  const body = {
    seed_type: seedType.value,
    seed_value: seedValue.value || '（在此填写查询目标）',
    platforms: plats.length ? plats : ['threatbook', 'qianxin'],
    reuse_cache: reuseCache.value,
  }
  if (informationJson.value && Object.keys(informationJson.value).length > 0) {
    body.information_json = informationJson.value
  }
  if (callbackEnabled.value) {
    body.callback = {
      url: callbackUrl.value.trim() || defaultCallbackUrl.value,
      method: callbackMethod.value,
      timeout: callbackTimeout.value,
    }
    if (callbackHeadersEnabled.value && Object.keys(callbackHeaders.value).length > 0) {
      body.callback.headers = callbackHeaders.value
    }
  }
  return JSON.stringify(body, null, 2)
})

const curlStep1 = computed(() =>
  `curl -X POST "${apiBase.value}/ti/expand" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <token>" \\
  -d '${step1Body.value.replace(/'/g, "'\\''")}'`
)

const curlStep2 = computed(() => {
  const id = taskId.value || '<task_id>'
  return `curl "${apiBase.value}/ti/tasks/${id}" \\
  -H "Authorization: Bearer <token>"`
})

// ─── Step 1：提交查询 ──────────────────────────────────────────────────────
const step1Loading = ref(false)
const step1Res     = ref(null)
const step1Error   = ref('')
const submitTasks  = ref([])  // [{task_id, platform, cached}]

async function readResponseData(res) {
  const text = await res.text()
  try { return { data: JSON.parse(text), text } } catch { return { data: null, text } }
}

async function runStep1() {
  step1Error.value = ''
  step2Error.value = ''
  step1Res.value   = null
  step2Res.value   = null
  submitTasks.value = []
  taskId.value = ''
  const value = seedValue.value.trim()
  if (!value) { step1Error.value = '请输入查询目标'; return }
  const plats = Object.keys(platforms.value).filter(p => platforms.value[p])
  if (!plats.length) { step1Error.value = '请至少选择一个平台'; return }

  step1Loading.value = true
  try {
    const body = {
      seed_type: seedType.value,
      seed_value: value,
      platforms: plats,
      reuse_cache: reuseCache.value,
    }
    if (informationJson.value && Object.keys(informationJson.value).length > 0) {
      body.information_json = informationJson.value
    }
    if (callbackEnabled.value) {
      body.callback = {
        url: callbackUrl.value.trim() || defaultCallbackUrl.value,
        method: callbackMethod.value,
        timeout: callbackTimeout.value,
      }
      if (callbackHeadersEnabled.value && Object.keys(callbackHeaders.value).length > 0) {
        body.callback.headers = callbackHeaders.value
      }
    }
    const res = await authFetch('/ti/expand', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const { data, text } = await readResponseData(res)
    step1Res.value = data ?? text
    if (!res.ok) {
      step1Error.value = data?.detail || `HTTP ${res.status}: ${text.slice(0, 300) || res.statusText}`
      return
    }
    submitTasks.value = data.tasks || []
    if (submitTasks.value.length) {
      taskId.value = submitTasks.value[0].task_id
      pollStep2()
    }
  } catch (e) {
    step1Error.value = e.message
  } finally {
    step1Loading.value = false
  }
}

// ─── Step 2：轮询任务结果 ───────────────────────────────────────────────────
const taskId       = ref('')
const step2Loading = ref(false)
const step2Res     = ref(null)
const step2Error   = ref('')
let pollTimer = null

function pollStep2() {
  clearTimeout(pollTimer)
  runStep2(true)
}

async function runStep2(retry = false) {
  if (!taskId.value) { step2Error.value = '请先执行 Step 1 获取 task_id'; return }
  step2Error.value = ''
  step2Loading.value = true
  try {
    const res = await authFetch(`/ti/tasks/${taskId.value}`)
    const { data, text } = await readResponseData(res)
    step2Res.value = data ?? text
    if (!res.ok) {
      step2Error.value = data?.detail || `HTTP ${res.status}: ${text.slice(0, 300) || res.statusText}`
      return
    }
    if (retry && (data.status === 'pending' || data.status === 'running')) {
      pollTimer = setTimeout(() => runStep2(true), 2500)
    } else {
      refreshSessions()  // 任务终态后刷新红绿灯
    }
  } catch (e) {
    step2Error.value = e.message
  } finally {
    step2Loading.value = false
  }
}

// 跳转到「解析详情 · 联网情报」查看平台原生风格展示
function openTiDetail() {
  if (taskId.value) emit('open-graph', taskId.value)
}

// ─── 会话红绿灯 ────────────────────────────────────────────────────────────
async function refreshSessions() {
  try {
    const res = await authFetch('/ti/sessions')
    if (res.ok) sessions.value = (await res.json()).items || []
  } catch (e) { /* 元信息失败不影响主流程 */ }
}

// ─── 人工登录（红灯 → 打开登录窗口/远程桌面）─────────────────────────────────
const loginPending = ref({})  // platform → 是否等待人工登录中
const loginHint = ref('')
let loginPollTimer = null

// 远程登录桌面弹窗（无显示器服务器：noVNC 网页远程浏览器）
const viewerUrl = ref('')       // iframe 地址（同源 /ti/login-view/...）
const viewerPlatform = ref('')  // 展示用平台名

function openLoginViewer(url, name) {
  viewerUrl.value = location.origin + url
  viewerPlatform.value = name
}

function closeLoginViewer() {
  viewerUrl.value = ''
  viewerPlatform.value = ''
}

async function startManualLogin(s) {
  try {
    const res = await authFetch(`/ti/sessions/${s.platform}/manual-login`, { method: 'POST' })
    const data = await res.json().catch(() => ({}))
    if (res.status === 409) {
      // 窗口已开：vnc 模式则补开远程桌面弹窗（刷新/换机后可重新接入）
      if (data.viewer_url) {
        openLoginViewer(data.viewer_url, s.name)
        loginHint.value = `${s.name} 远程登录桌面已重新接入，请在弹出的窗口中完成登录`
      } else {
        loginHint.value = `${s.name} 登录窗口已打开，请到弹出的浏览器窗口中完成登录`
      }
    } else if (!res.ok) {
      loginHint.value = `打开登录窗口失败：${data.detail || res.status}`
      return
    } else if (data.mode === 'vnc' && data.viewer_url) {
      openLoginViewer(data.viewer_url, s.name)
      loginHint.value = `${s.name} 远程登录桌面已打开，请在弹出的窗口中完成登录（滑块/扫码/密码）；系统会做数据面验证，通过后状态自动变绿`
    } else {
      loginHint.value = `${s.name} 登录窗口已在服务器本机打开，请前往完成登录（滑块/扫码/密码）；通过后窗口自动关闭、状态变绿`
    }
    loginPending.value = { ...loginPending.value, [s.platform]: true }
    pollLoginState(s.platform)
  } catch (e) {
    loginHint.value = `打开登录窗口失败：${e.message}`
  }
}

// 登录完成后端自动接管会话并置 ok；每 5s 轮询红绿灯直至变绿（最长 10 分钟）
function pollLoginState(platform) {
  clearTimeout(loginPollTimer)
  let elapsed = 0
  const tick = async () => {
    await refreshSessions()
    const cur = sessions.value.find(x => x.platform === platform)
    if (cur && cur.login_state === 'ok') {
      loginPending.value = { ...loginPending.value, [platform]: false }
      loginHint.value = `${cur.name} 会话已恢复`
      closeLoginViewer()
      setTimeout(() => { loginHint.value = '' }, 5000)
      return
    }
    elapsed += 5000
    if (elapsed >= 10 * 60 * 1000) {
      loginPending.value = { ...loginPending.value, [platform]: false }
      loginHint.value = '等待登录超时；如仍在操作可忽略本提示，完成后刷新页面即可'
      return
    }
    loginPollTimer = setTimeout(tick, 5000)
  }
  loginPollTimer = setTimeout(tick, 5000)
}

// ─── 导入会话 Cookie（兜底登录：本机浏览器登录平台后粘贴 cookie）──────────────
const importTarget = ref(null)  // 目标平台会话对象
const importText = ref('')
const importBusy = ref(false)
const importMsg = ref('')
const importOk = ref(false)

function openImport(s) {
  importTarget.value = s
  importText.value = ''
  importMsg.value = ''
  importOk.value = false
}

function closeImport() {
  importTarget.value = null
}

async function submitImport() {
  const s = importTarget.value
  if (!s || !importText.value.trim()) return
  importBusy.value = true
  importMsg.value = ''
  try {
    const res = await authFetch(`/ti/sessions/${s.platform}/import-cookies`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cookies: importText.value.trim(),
        user_agent: navigator.userAgent,  // cookie 就是在本浏览器登录的，UA 须一致
      }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      importOk.value = false
      importMsg.value = data.detail || `HTTP ${res.status}`
      return
    }
    importOk.value = true
    importMsg.value = `导入成功：${data.cookies} 条 cookie，数据面验证已通过`
    await refreshSessions()
    setTimeout(closeImport, 1500)
  } catch (e) {
    importOk.value = false
    importMsg.value = e.message
  } finally {
    importBusy.value = false
  }
}

// ─── 工具 ──────────────────────────────────────────────────────────────────
const copiedKey = ref('')
async function copyToClipboard(text, key) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0'
      document.body.appendChild(ta)
      ta.focus()
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    copiedKey.value = key
    setTimeout(() => { if (copiedKey.value === key) copiedKey.value = '' }, 1800)
  } catch (e) {
    console.warn('复制失败:', e)
  }
}

function taskStatusLabel(s) {
  return { pending: '排队中', running: '查询中', done: 'done', failed: 'failed' }[s] || s
}

onMounted(async () => {
  refreshSessions()
  try {
    const res = await authFetch('/server-info')
    const data = await res.json()
    backendBase.value = data.base_url || ''
    if (!callbackUrl.value && defaultCallbackUrl.value) {
      callbackUrl.value = defaultCallbackUrl.value
    }
  } catch {}
})
onUnmounted(() => { clearTimeout(pollTimer); clearTimeout(loginPollTimer) })
</script>

<template>
  <div class="playground">
    <h2>联网情报调用台</h2>
    <p class="subtitle">
      以浏览器采集微步在线 / 奇安信的平台原生内容（IP / 域名 / 样本哈希），
      同平台串行 + 随机间隔（反爬纪律），任务耗时通常 10~40 秒
    </p>

    <!-- 平台会话状态条 -->
    <div class="session-bar">
      <span v-for="s in sessions" :key="s.platform" class="session-chip"
            :class="(SESSION_STATES[s.login_state] || {}).cls">
        {{ s.name }}：{{ (SESSION_STATES[s.login_state] || {}).label || s.login_state }}
        <button v-if="s.login_state !== 'ok'" class="login-btn"
                :disabled="!!loginPending[s.platform]"
                @click="startManualLogin(s)">
          {{ loginPending[s.platform] ? '等待登录…' : '去登录' }}
        </button>
        <button v-if="s.login_state !== 'ok'" class="login-btn"
                title="本机浏览器登录平台后粘贴 Cookie 恢复会话（兜底方式）"
                @click="openImport(s)">
          导入Cookie
        </button>
        <button v-if="s.login_state === 'ok'" class="login-btn"
                :disabled="!!loginPending[s.platform]"
                title="会话当前有效；如需更换账号或提前刷新登录态，可重新登录"
                @click="startManualLogin(s)">
          {{ loginPending[s.platform] ? '等待登录…' : '重新登录' }}
        </button>
      </span>
    </div>
    <p v-if="loginHint" class="login-hint">{{ loginHint }}</p>

    <!-- ═══════ 参数配置区 ═══════ -->
    <section class="card params-card">
      <h3>请求参数</h3>
      <div class="form-grid">
        <div class="field">
          <label>API 地址</label>
          <input v-model="apiBase" placeholder="http://localhost:8036" />
        </div>

        <div class="field">
          <label>查询类型</label>
          <select v-model="seedType">
            <option v-for="t in SEED_TYPES" :key="t.value" :value="t.value">{{ t.label }}</option>
          </select>
        </div>

        <div class="field full">
          <label>查询目标</label>
          <input v-model="seedValue" :placeholder="seedPlaceholder()" @keyup.enter="runStep1" />
        </div>

        <div class="field">
          <label>目标平台</label>
          <div class="plat-row">
            <label v-for="p in Object.keys(PLATFORM_LABELS)" :key="p" class="plat-check">
              <input type="checkbox" v-model="platforms[p]" /> {{ PLATFORM_LABELS[p] }}
            </label>
          </div>
        </div>

        <div class="field">
          <label class="toggle-label">
            <span class="toggle-text">启用缓存复用</span>
            <span class="toggle-hint">（TTL 内同种子同平台直接命中历史结果）</span>
            <label class="switch">
              <input type="checkbox" v-model="reuseCache" />
              <span class="slider"></span>
            </label>
          </label>
        </div>

        <!-- information_json 消息绑定（与 NER/ATT&CK 调用台一致） -->
        <div class="field full">
          <div class="label-with-action">
            <div class="label-with-help">
              <label>
                information_json
                <small>（消息绑定）</small>
              </label>
              <span
                class="help-icon"
                title="用于绑定自定义信息，会在结果与回调中原封不动返回&#10;示例：{&quot;file_id&quot;: &quot;0537ef0d8ac6a2179a58067615825924&quot;}&#10;• 可用于追踪请求来源、文件标识等"
              >?</span>
            </div>
            <button class="format-btn" @click="formatInformation" type="button" title="格式化 JSON">
              格式化
            </button>
          </div>
          <textarea
            v-model="informationStr"
            :class="{ 'input-error': informationError, 'json-editor': true }"
            rows="4"
            placeholder='{"file_id": "0537ef0d8ac6a2179a58067615825924"}'
            spellcheck="false"
          />
          <span v-if="informationError" class="field-error">{{ informationError }}</span>
          <span class="field-hint">💡 可选，原封不动返回</span>
        </div>

        <!-- 回调配置（与 NER/ATT&CK 调用台一致） -->
        <div class="field full callback-section">
          <div class="callback-header">
            <div class="label-with-help">
              <label>回调配置 <small>（可选）</small></label>
              <span
                class="help-icon"
                title="任务完成后自动推送结果到指定地址&#10;注意：回调失败不会自动重试&#10;• 可在回调失败管理中查看失败记录&#10;• 支持手动重试失败的回调"
              >?</span>
            </div>
            <button
              :class="['toggle-callback-btn', { active: callbackEnabled }]"
              @click="callbackEnabled = !callbackEnabled"
              type="button"
            >{{ callbackEnabled ? '✓ 已启用' : '启用回调' }}</button>
          </div>

          <div v-if="!callbackEnabled" class="disabled-placeholder callback-placeholder">
            <div class="placeholder-content">
              <span class="placeholder-icon">📤</span>
              <span class="placeholder-text">回调功能未启用</span>
              <span class="placeholder-hint">启用后可自动推送结果，无需轮询</span>
            </div>
          </div>

          <div v-else class="callback-config">
            <div class="callback-row">
              <div class="field">
                <label>回调地址（可选，留空使用默认测试端点）</label>
                <input v-model="callbackUrl" :placeholder="defaultCallbackUrl" type="url" />
                <span class="field-hint">💡 留空将使用系统内置测试端点，回调数据会在后端控制台打印</span>
              </div>
              <div class="field callback-method">
                <label>请求方式</label>
                <select v-model="callbackMethod">
                  <option value="POST">POST</option>
                  <option value="PUT">PUT</option>
                </select>
              </div>
              <div class="field callback-timeout">
                <label>超时时间（秒）</label>
                <input v-model.number="callbackTimeout" type="number" min="1" max="300" />
              </div>
            </div>

            <div class="field">
              <div class="label-with-action">
                <div class="label-with-help">
                  <label>自定义请求头 <small>（可选，JSON 对象）</small></label>
                </div>
                <div class="mapping-controls">
                  <button
                    :class="['toggle-mapping-btn', { active: callbackHeadersEnabled }]"
                    @click="callbackHeadersEnabled = !callbackHeadersEnabled"
                    type="button"
                  >
                    {{ callbackHeadersEnabled ? '✓ 已启用' : '启用请求头' }}
                  </button>
                  <button
                    v-if="callbackHeadersEnabled"
                    class="format-btn"
                    @click="formatCallbackHeaders"
                    type="button"
                    title="格式化 JSON"
                  >
                    格式化
                  </button>
                </div>
              </div>
              <div v-if="!callbackHeadersEnabled" class="disabled-placeholder" style="min-height: calc(4 * 1.5em + 14px);">
                <div class="placeholder-content">
                  <span class="placeholder-icon">🔒</span>
                  <span class="placeholder-text">自定义请求头未启用</span>
                  <span class="placeholder-hint">点击上方"启用请求头"按钮开启</span>
                </div>
              </div>
              <template v-else>
                <textarea
                  v-model="callbackHeadersStr"
                  :class="{ 'input-error': callbackHeadersError, 'json-editor': true }"
                  rows="4"
                  placeholder='{"Authorization": "Bearer xxx", "X-Custom": "value"}'
                  spellcheck="false"
                />
                <span v-if="callbackHeadersError" class="field-error">{{ callbackHeadersError }}</span>
              </template>
            </div>

            <div class="callback-notice">
              <div class="notice-item">
                💡 <strong>默认测试端点：</strong>留空回调地址将使用系统内置端点 <code>{{ defaultCallbackUrl }}</code>，回调数据会在后端控制台打印，方便测试。
              </div>
              <div class="notice-item">
                ⚠️ <strong>注意：</strong>回调失败不会自动重试，请在"回调失败管理"中查看失败记录并手动重试。
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ═══════ 两步请求并排 ═══════ -->
    <div class="steps-row">
      <!-- ── Step 1 ── -->
      <section class="card step-card">
        <div class="step-header">
          <span class="step-badge">Step 1</span>
          <span class="step-title">POST /ti/expand &nbsp;→ 获取 task_id</span>
        </div>

        <div class="curl-block">
          <div class="curl-topbar">
            <span>curl</span>
            <button class="copy-btn" :class="{ copied: copiedKey === 'step1' }"
                    @click="copyToClipboard(curlStep1, 'step1')">
              {{ copiedKey === 'step1' ? '✓ 已复制' : '复制' }}
            </button>
          </div>
          <pre class="curl-pre" v-html="highlightCurl(curlStep1)"></pre>
        </div>

        <button class="run-btn"
                :disabled="step1Loading || !!informationError || !seedValue.trim()"
                @click="runStep1">
          <span v-if="step1Loading" class="spinner"></span>
          {{ step1Loading ? '请求中…' : '▶ 执行请求' }}
        </button>

        <div v-if="step1Error" class="resp-error">{{ step1Error }}</div>
        <div v-if="step1Res" class="resp-block">
          <div class="resp-topbar">
            <span class="resp-label">Response</span>
            <span v-for="t in submitTasks" :key="t.task_id" class="task-id-badge">
              {{ PLATFORM_LABELS[t.platform] }}: {{ t.task_id.slice(0, 8) }}…
              <template v-if="t.cached">（缓存命中）</template>
            </span>
          </div>
          <pre class="resp-pre" v-html="highlightJson(step1Res)"></pre>
        </div>
      </section>

      <!-- ── Step 2 ── -->
      <section class="card step-card">
        <div class="step-header">
          <span class="step-badge step-badge-2">Step 2</span>
          <span class="step-title">GET /ti/tasks/{task_id} &nbsp;→ 查询情报结果</span>
        </div>

        <div class="taskid-row">
          <label>task_id</label>
          <select v-if="submitTasks.length > 1" v-model="taskId" @change="runStep2(true)">
            <option v-for="t in submitTasks" :key="t.task_id" :value="t.task_id">
              {{ PLATFORM_LABELS[t.platform] }} · {{ t.task_id.slice(0, 8) }}…
            </option>
          </select>
          <input v-else v-model="taskId" placeholder="执行 Step 1 后自动填入，或手动输入" />
        </div>

        <div class="curl-block">
          <div class="curl-topbar">
            <span>curl</span>
            <button class="copy-btn" :class="{ copied: copiedKey === 'step2' }"
                    @click="copyToClipboard(curlStep2, 'step2')">
              {{ copiedKey === 'step2' ? '✓ 已复制' : '复制' }}
            </button>
          </div>
          <pre class="curl-pre" v-html="highlightCurl(curlStep2)"></pre>
        </div>

        <div class="step2-actions">
          <button class="run-btn" :disabled="step2Loading || !taskId.trim()" @click="runStep2(false)">
            <span v-if="step2Loading" class="spinner"></span>
            {{ step2Loading ? '查询中…' : '▶ 执行查询' }}
          </button>
          <button v-if="step2Res?.status === 'done'" class="graph-btn" @click="openTiDetail">
            🌐 查看情报展示
          </button>
        </div>

        <div v-if="step2Error" class="resp-error">{{ step2Error }}</div>
        <div v-if="step2Res" class="resp-block">
          <div class="resp-topbar">
            <span class="resp-label">Response</span>
            <span :class="['status-badge', step2Res.status]">{{ taskStatusLabel(step2Res.status) }}</span>
            <span v-if="step2Res.platform" class="plat-badge">{{ PLATFORM_LABELS[step2Res.platform] || step2Res.platform }}</span>
            <span v-if="step2Res.latency_ms != null" class="latency">{{ step2Res.latency_ms?.toFixed(0) }} ms</span>
            <span v-if="step2Res.sections?.length" class="entity-count">{{ step2Res.sections.length }} 个区块</span>
            <span v-if="step2Res.information_json && Object.keys(step2Res.information_json).length" class="info-badge" title="包含自定义信息">📎 Info</span>
            <button class="copy-btn resp-copy-btn" :class="{ copied: copiedKey === 'step2-resp' }"
                    @click="copyToClipboard(JSON.stringify(step2Res, null, 2), 'step2-resp')"
                    title="复制完整 JSON 响应">
              {{ copiedKey === 'step2-resp' ? '✓ 已复制' : '复制 JSON' }}
            </button>
          </div>
          <div v-if="step2Res.status === 'pending' || step2Res.status === 'running'" class="polling-hint">
            <span class="spinner"></span> 浏览器采集中（同平台串行 + 随机间隔），自动轮询…
          </div>
          <div v-if="step2Res.status === 'done' && step2Res.sections?.length" class="entity-preview">
            <span v-for="(s, i) in step2Res.sections" :key="i" class="ent-chip">{{ s.name }}</span>
          </div>
          <pre class="resp-pre" v-html="highlightJson(step2Res)"></pre>
        </div>
      </section>
    </div>

    <!-- ── 远程登录桌面弹窗（noVNC 网页远程浏览器，无显示器服务器适用）── -->
    <div v-if="viewerUrl" class="modal-mask" @click.self="closeLoginViewer">
      <div class="modal viewer-modal">
        <div class="modal-header">
          <span class="modal-title">{{ viewerPlatform }} · 远程登录桌面</span>
          <button class="modal-close" @click="closeLoginViewer" title="关闭弹窗不影响登录进程">✕</button>
        </div>
        <p class="modal-tip">
          窗口里是服务器上的浏览器画面，请直接在其中完成登录（滑块/扫码/密码）；
          系统会自动做数据面验证，通过后桌面关闭、状态变绿。关掉本弹窗不影响登录进程。
        </p>
        <iframe :src="viewerUrl" class="viewer-frame" title="远程登录桌面"></iframe>
      </div>
    </div>

    <!-- ── 导入会话 Cookie 弹窗 ── -->
    <div v-if="importTarget" class="modal-mask" @click.self="closeImport">
      <div class="modal import-modal">
        <div class="modal-header">
          <span class="modal-title">{{ importTarget.name }} · 导入会话 Cookie</span>
          <button class="modal-close" @click="closeImport">✕</button>
        </div>
        <div class="import-steps">
          <p>1）在<b>你自己的浏览器</b>里正常登录 {{ importTarget.name }} 官网（滑块/验证码就地完成）；</p>
          <p>2）导出 Cookie，二选一：</p>
          <p class="indent">· 用 Cookie-Editor 插件 →「导出」→ JSON；</p>
          <p class="indent">· 或 F12 → Network → 刷新页面 → 点任意请求 → 复制请求头里的 <code>Cookie</code> 整串；</p>
          <p>3）粘贴到下面提交。服务端会做数据面验证，<b>通过才接管会话</b>（自动带上你当前浏览器的 UA）。</p>
        </div>
        <textarea v-model="importText" class="import-textarea" rows="7" spellcheck="false"
                  placeholder='[{"name":"...","value":"...","domain":".threatbook.com",...}]  或  k1=v1; k2=v2'></textarea>
        <div v-if="importMsg" class="import-msg" :class="{ ok: importOk }">{{ importMsg }}</div>
        <div class="modal-actions">
          <button class="run-btn" :disabled="importBusy || !importText.trim()" @click="submitImport">
            <span v-if="importBusy" class="spinner"></span>
            {{ importBusy ? '验证中…' : '提交并验证' }}
          </button>
          <button class="cancel-btn" @click="closeImport">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.playground { max-width: 100%; margin: 0; padding: 0; }
h2 { font-size: 18px; margin-bottom: 4px; }
.subtitle { color: #888; font-size: 13px; margin-bottom: 12px; }
h3 { font-size: 15px; margin-bottom: 14px; color: #444; }

/* ── 会话红绿灯 ── */
.session-bar { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.session-chip { font-size: 13px; padding: 4px 12px; border-radius: 12px; background: #eceff1; color: #555; }
.session-chip.ok   { background: #e8f5e9; color: #2e7d32; }
.session-chip.warn { background: #fff3e0; color: #e65100; }
.session-chip.bad  { background: #ffebee; color: #c62828; }
.login-btn { margin-left: 8px; font-size: 12px; padding: 1px 10px; border: 1px solid currentColor;
  border-radius: 10px; background: transparent; color: inherit; cursor: pointer; }
.login-btn:hover:not(:disabled) { background: rgba(198, 40, 40, .08); }
.login-btn:disabled { opacity: .6; cursor: default; }
.login-hint { font-size: 13px; color: #e65100; margin: -6px 0 12px; }

/* ── 卡片 / 表单（与 NER 调用台一致）── */
.card { background: #fff; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 16px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field.full { grid-column: 1 / -1; }
.field label { font-size: 13px; font-weight: 600; color: #555; }
.field label small { font-weight: 400; color: #999; }
.field input, .field select, .field textarea {
  padding: 8px 12px; border: 1px solid #d0d0d0; border-radius: 6px; font-size: 14px; outline: none;
}
.field input:focus, .field select:focus { border-color: #5470c6; }

.plat-row { display: flex; gap: 16px; align-items: center; height: 35px; }
.plat-check { font-size: 14px; color: #444; display: flex; align-items: center; gap: 4px; white-space: nowrap; font-weight: 400 !important; }

.toggle-label { display: flex; align-items: center; gap: 12px; user-select: none; }
.toggle-text { color: #555; font-weight: 600; font-size: 13px; }
.toggle-hint { color: #999; font-size: 12px; font-weight: 400; flex: 1; }
.switch { position: relative; display: inline-block; width: 48px; height: 24px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #ccc; transition: .3s; border-radius: 24px; }
.slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }
.switch input:checked + .slider { background-color: #5470c6; }

/* ── 回调配置（与 NER/ATT&CK 调用台一致）── */
.callback-section { border-top: 2px solid #f0f0f0; padding-top: 20px; margin-top: 20px; }
.callback-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.toggle-callback-btn {
  background: #e0e0e0; color: #666; border: 1px solid #d0d0d0; border-radius: 4px;
  padding: 6px 16px; font-size: 13px; cursor: pointer; transition: all 0.2s;
  white-space: nowrap; font-weight: 500;
}
.toggle-callback-btn:hover { background: #d0d0d0; border-color: #c0c0c0; }
.toggle-callback-btn.active { background: #5470c6; color: #fff; border-color: #4a63b3; }
.toggle-callback-btn.active:hover { background: #4a63b3; border-color: #3f56a0; }
.callback-placeholder { min-height: 120px; }
.callback-config {
  display: flex; flex-direction: column; gap: 14px; padding: 16px;
  background: #f8f9fa; border-radius: 6px; border: 1px solid #e0e0e0;
}
.callback-row { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 14px; }
.callback-method { min-width: 120px; }
.callback-timeout { min-width: 140px; }
.callback-notice {
  background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 6px;
  padding: 12px 16px; font-size: 13px; color: #555; line-height: 1.6;
  display: flex; flex-direction: column; gap: 8px;
}
.callback-notice .notice-item { display: flex; align-items: flex-start; gap: 8px; }
.callback-notice .notice-item strong { color: #333; }
.callback-notice .notice-item code {
  background: #e0e0e0; padding: 2px 6px; border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; color: #5470c6;
}

/* 标签行 / 开关按钮 / 编辑器（移植自 ApiPlayground） */
.label-with-action { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.label-with-help { display: flex; align-items: center; gap: 6px; }
.mapping-controls { display: flex; align-items: center; gap: 8px; }
.toggle-mapping-btn {
  background: #e0e0e0; color: #666; border: 1px solid #d0d0d0; border-radius: 4px;
  padding: 4px 12px; font-size: 12px; cursor: pointer; transition: all 0.2s;
  white-space: nowrap; font-weight: 500;
}
.toggle-mapping-btn:hover { background: #d0d0d0; border-color: #c0c0c0; }
.toggle-mapping-btn.active { background: #3ba272; color: #fff; border-color: #2e8060; }
.toggle-mapping-btn.active:hover { background: #2e8060; border-color: #256b4f; }
.disabled-placeholder {
  border: 1px solid #ddd; border-radius: 6px;
  background: repeating-linear-gradient(45deg, #f8f8f8, #f8f8f8 10px, #f0f0f0 10px, #f0f0f0 20px);
  display: flex; align-items: center; justify-content: center;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace; font-size: 13px;
  line-height: 1.5; padding: 7px 10px; min-height: calc(8 * 1.5em + 14px);
}
.placeholder-content { display: flex; flex-direction: column; align-items: center; gap: 8px; color: #999; }
.placeholder-icon { font-size: 32px; opacity: 0.5; }
.placeholder-text { font-size: 14px; font-weight: 600; color: #888; }
.placeholder-hint { font-size: 12px; color: #aaa; }
.help-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; background: #5470c6; color: #fff; border-radius: 50%;
  font-size: 11px; font-weight: bold; cursor: help; flex-shrink: 0;
  transition: all 0.2s; font-family: Arial, sans-serif;
}
.help-icon:hover { background: #3f56a0; transform: scale(1.1); }
.format-btn {
  background: #5470c6; color: #fff; border: 1px solid #4a63b3; border-radius: 4px;
  padding: 4px 10px; font-size: 12px; cursor: pointer; transition: all 0.2s;
  white-space: nowrap; font-weight: 500;
}
.format-btn:hover { background: #4a63b3; border-color: #3f56a0; }
.format-btn:active { background: #3f56a0; transform: scale(0.98); }
.field textarea { resize: vertical; }
.json-editor {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px; line-height: 1.5; background: #f8f9fa; tab-size: 2;
}
.input-error { border-color: #e74c3c !important; }
.field-error { color: #e74c3c; font-size: 12px; }
.field-hint { color: #3ba272; font-size: 12px; margin-top: 2px; }

/* ── 两步卡片 ── */
.steps-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.step-card { display: flex; flex-direction: column; }
.step-header { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.step-badge { background: #5470c6; color: #fff; font-size: 12px; font-weight: 600; padding: 2px 10px; border-radius: 10px; }
.step-badge-2 { background: #3ba272; }
.step-title { font-size: 14px; font-weight: 600; color: #444; }

.curl-block { border: 1px solid #2d2d2d; border-radius: 6px; overflow: hidden; margin-bottom: 12px; }
.curl-topbar { display: flex; justify-content: space-between; align-items: center; background: #2d2d2d; color: #aaa; font-size: 12px; padding: 4px 10px; }
.curl-pre { background: #1e1e1e; color: #d4d4d4; margin: 0; padding: 10px 12px; font-size: 12px; white-space: pre-wrap; word-break: break-all; max-height: 220px; overflow: auto; }
/* curl 高亮 token（v-html 注入的 span 不带 scoped 属性，必须 :deep；与 NER/ATT&CK 调用台同一套 monokai 配色） */
.curl-pre :deep(.ck-c)  { color: #f8f8f2; font-weight: 700; }   /* curl 命令 */
.curl-pre :deep(.ck-m)  { color: #ae81ff; }                     /* HTTP 方法 */
.curl-pre :deep(.ck-f)  { color: #f92672; }                     /* -X/-H/-d 等旗标 */
.curl-pre :deep(.ck-u)  { color: #66d9ef; }                     /* URL */
.curl-pre :deep(.ck-s)  { color: #a6e22e; }                     /* 引号字符串 */
.curl-pre :deep(.ck-ct) { color: #75715e; }                     /* 行尾续行符 */
.curl-pre :deep(.ck-hk) { color: #66d9ef; }                     /* -H 头部名（值按 ck-s 字符串色） */
/* -d JSON 请求体 token（monokai，与 NER/ATT&CK 调用台一致） */
.curl-pre :deep(.k)  { color: #66d9ef; }
.curl-pre :deep(.s)  { color: #a6e22e; }
.curl-pre :deep(.n)  { color: #ae81ff; }
.curl-pre :deep(.b)  { color: #f92672; }
.curl-pre :deep(.nl) { color: #75715e; }
.copy-btn { background: transparent; border: 1px solid #555; color: #bbb; border-radius: 4px; font-size: 12px; padding: 2px 10px; cursor: pointer; }
.copy-btn:hover { border-color: #888; color: #fff; }
.copy-btn.copied { border-color: #3ba272; color: #3ba272; }

.run-btn { background: #5470c6; color: #fff; border: none; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; justify-content: center; }
.run-btn:disabled { opacity: .55; cursor: not-allowed; }
.step2-actions { display: flex; gap: 10px; }
.step2-actions .run-btn { flex: 1; }
.graph-btn { background: #3ba272; color: #fff; border: none; border-radius: 6px; padding: 9px 16px; font-size: 14px; cursor: pointer; white-space: nowrap; }

.spinner { width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.35); border-top-color: #fff; border-radius: 50%; animation: spin .7s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }

.taskid-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.taskid-row label { font-size: 13px; font-weight: 600; color: #555; white-space: nowrap; }
.taskid-row input, .taskid-row select { flex: 1; padding: 8px 12px; border: 1px solid #d0d0d0; border-radius: 6px; font-size: 13px; outline: none; }

.resp-error { margin-top: 10px; color: #c62828; font-size: 13px; }
.resp-block { margin-top: 12px; border: 1px solid #e0e0e0; border-radius: 6px; overflow: hidden; }
.resp-topbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; background: #f5f7fa; padding: 6px 10px; border-bottom: 1px solid #e8e8e8; }
.resp-label { font-size: 12px; font-weight: 600; color: #888; }
.task-id-badge { font-size: 12px; color: #5470c6; background: #e8f0fe; border-radius: 10px; padding: 1px 10px; }
.status-badge { font-size: 12px; border-radius: 10px; padding: 1px 10px; background: #eceff1; color: #555; }
.status-badge.done { background: #e8f5e9; color: #2e7d32; }
.status-badge.failed { background: #ffebee; color: #c62828; }
.status-badge.running { background: #fff3e0; color: #e65100; }
.plat-badge { font-size: 12px; color: #6a3fb5; background: #f3e5f5; border-radius: 10px; padding: 1px 10px; }
.latency, .entity-count { font-size: 12px; color: #888; }
.info-badge { background: #f59e0b; color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; cursor: help; }
.resp-copy-btn { margin-left: auto; border-color: #d0d0d0; color: #666; }
.resp-pre { margin: 0; padding: 10px 12px; font-size: 12px; max-height: 360px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
.resp-pre :deep(.k) { color: #0451a5; }
.resp-pre :deep(.s) { color: #a31515; }
.resp-pre :deep(.n) { color: #098658; }
.resp-pre :deep(.b), .resp-pre :deep(.nl) { color: #0000ff; }

.polling-hint { display: flex; align-items: center; gap: 8px; padding: 10px 12px; font-size: 13px; color: #e65100; }
.polling-hint .spinner { border-color: rgba(230,81,0,.25); border-top-color: #e65100; }
.entity-preview { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 12px; border-bottom: 1px solid #f0f0f0; }
.ent-chip { font-size: 12px; background: #eef2fb; color: #3b5bbd; border-radius: 4px; padding: 2px 8px; }

/* ── 弹窗（远程登录桌面 / 导入 Cookie）── */
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 1000; display: flex; align-items: center; justify-content: center; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; box-shadow: 0 8px 30px rgba(0,0,0,.25); display: flex; flex-direction: column; }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.modal-title { font-size: 15px; font-weight: 600; color: #333; }
.modal-close { background: none; border: none; font-size: 16px; color: #999; cursor: pointer; padding: 2px 6px; }
.modal-close:hover { color: #333; }
.modal-tip { font-size: 12px; color: #888; line-height: 1.6; margin: 0 0 10px; }
.viewer-modal { width: min(1180px, 94vw); }
.viewer-frame { width: 100%; height: min(72vh, 760px); border: 1px solid #e0e0e0; border-radius: 6px; background: #2d2d2d; }
.import-modal { width: min(640px, 92vw); }
.import-steps { font-size: 13px; color: #555; line-height: 1.7; margin-bottom: 10px; }
.import-steps p { margin: 2px 0; }
.import-steps .indent { padding-left: 14px; color: #777; }
.import-steps code { background: #eceff1; padding: 1px 5px; border-radius: 3px; font-size: 12px; color: #c62828; }
.import-textarea { width: 100%; padding: 8px 12px; border: 1px solid #d0d0d0; border-radius: 6px; font-size: 12px; outline: none; resize: vertical; font-family: 'Consolas', 'Monaco', monospace; box-sizing: border-box; }
.import-textarea:focus { border-color: #5470c6; }
.import-msg { margin-top: 8px; font-size: 13px; color: #c62828; }
.import-msg.ok { color: #2e7d32; }
.modal-actions { display: flex; gap: 10px; margin-top: 14px; }
.modal-actions .run-btn { flex: 1; }
.cancel-btn { background: #eceff1; color: #555; border: none; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; }

@media (max-width: 1100px) {
  .steps-row { grid-template-columns: 1fr; }
  .callback-row { grid-template-columns: 1fr; }
}
</style>
