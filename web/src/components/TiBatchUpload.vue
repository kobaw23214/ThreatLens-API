<script setup>
/**
 * TI 联网解析批量任务：上传 JSON 提交 + 任务组管理
 * 弹窗两个 tab：
 *   「提交任务」选择文件（下载模版）→ 校验/预览（可改 options）→ 逐条提交（间隔 200ms）→ 跟踪进度
 *   「任务组」  历史批次列表：进度实时轮询、导出 SQL（管理员）、删除本地记录
 * 批次（任务组）记录存 localStorage（tiBatch.js），弹窗关闭不中断提交与跟踪，
 * 队列状态页（QueueStatus.vue）另有后台轮询做恢复跟踪。
 */
import { ref, computed, inject, watch, onUnmounted } from 'vue'
import { authFetch } from '../utils/auth.js'
import {
  ALL_PLATFORMS,
  downloadTemplate,
  validateTemplate,
  makeBatchId,
  saveBatch,
  removeBatch,
  listBatchesSorted,
  exportBatchSql,
} from '../utils/tiBatch.js'

const props = defineProps({
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'submitted'])

const PLATFORM_LABELS = { threatbook: '微步在线', qianxin: '奇安信' }
const SEED_TYPE_LABELS = { ip: 'IP', domain: '域名', file: '样本' }

// ─── 弹窗内 tab ──────────────────────────────────────────────────────────────
const modalTab = ref('submit')  // submit | groups

// ─── 步骤状态 ────────────────────────────────────────────────────────────────
// pick → preview → submitting → track
const step = ref('pick')

// ─── 选择区 ──────────────────────────────────────────────────────────────────
const fileInput = ref(null)
const sourceFile = ref('')
const fileErrors = ref([])  // 解析/校验错误列表（每条一行红字）

function pickFile() {
  fileInput.value?.click()
}

async function onFileChange(e) {
  const file = e.target.files?.[0]
  e.target.value = ''  // 允许重复选择同一文件
  if (!file) return
  sourceFile.value = file.name
  fileErrors.value = []
  let obj
  try {
    obj = JSON.parse(await file.text())
  } catch {
    fileErrors.value = ['JSON 解析失败：文件不是合法的 JSON']
    return
  }
  const { ok, errors, normalized } = validateTemplate(obj)
  if (!ok) {
    fileErrors.value = errors
    return
  }
  // 进入预览：options 初始值来自文件，提交前可改
  seeds.value = normalized.seeds
  platforms.value = {
    threatbook: normalized.options.platforms.includes('threatbook'),
    qianxin: normalized.options.platforms.includes('qianxin'),
  }
  reuseCache.value = normalized.options.reuse_cache
  step.value = 'preview'
}

// ─── 预览区 ──────────────────────────────────────────────────────────────────
const seeds = ref([])
const platforms = ref({ threatbook: true, qianxin: true })
const reuseCache = ref(true)

const selectedPlatforms = computed(() => ALL_PLATFORMS.filter(p => platforms.value[p]))
const seedCountByType = computed(() => {
  const c = { ip: 0, domain: 0, file: 0 }
  for (const s of seeds.value) c[s.seed_type] = (c[s.seed_type] || 0) + 1
  return c
})
const totalTaskCount = computed(() => seeds.value.length * selectedPlatforms.value.length)

// TI 队列余量提示（SSE 缺失时容错不显示）
const sseData = inject('sseData', ref(null))
const queueHints = computed(() => {
  const plats = sseData.value?.ti_queue?.platforms
  if (!plats) return []
  return Object.values(plats).map(p => ({
    platform: p.platform,
    name: PLATFORM_LABELS[p.platform] || p.platform,
    used: (p.queue_size ?? 0) + (p.busy ? 1 : 0),
    max: p.queue_max ?? '—',
  }))
})

// ─── 提交 ────────────────────────────────────────────────────────────────────
const batch = ref(null)          // 当前批次记录（提交开始时即落 localStorage，逐条追加）
const submittedSeeds = ref(0)    // 已处理的种子数（含失败）
const limitAbortMsg = ref('')    // 429 终止提示
const submitting = ref(false)

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

async function submitOneSeed(seed) {
  const body = {
    seed_type: seed.seed_type,
    seed_value: seed.seed_value,
    platforms: selectedPlatforms.value,
    reuse_cache: reuseCache.value,
    information_json: { batch_id: batch.value.batch_id, source: 'ti_batch_upload' },
  }
  // 503（队列满 / TI 未启用）等待 10s 重试，单种子最多重试 5 次
  for (let attempt = 0; attempt <= 5; attempt++) {
    const res = await authFetch('/ti/expand', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (res.status === 503) {
      if (attempt < 5) { await sleep(10000); continue }
      const data = await res.json().catch(() => ({}))
      return { status: 503, error: data.detail || '队列已满或服务未启用（重试 5 次仍失败）' }
    }
    if (res.status === 429) return { status: 429, error: '用户调用限额已用尽' }
    const data = await res.json().catch(() => ({}))
    if (res.status === 422) return { status: 422, error: data.detail ? JSON.stringify(data.detail) : '种子非法' }
    if (!res.ok) return { status: res.status, error: data.detail || `HTTP ${res.status}` }
    return { status: res.status, tasks: data.tasks || [] }
  }
}

async function submitBatch() {
  if (!seeds.value.length || !selectedPlatforms.value.length) return
  submitting.value = true
  limitAbortMsg.value = ''
  batch.value = {
    batch_id: makeBatchId(),
    created_at: new Date().toISOString(),
    source_file: sourceFile.value,
    options: {
      platforms: [...selectedPlatforms.value],
      reuse_cache: reuseCache.value,
    },
    tasks: [],
    submit_failures: [],
    submit_done: false,  // 提交完成前不允许任何人把批次标记为 finished
    finished: false,
  }
  saveBatch(batch.value)
  emit('submitted', batch.value)
  submittedSeeds.value = 0
  step.value = 'submitting'

  for (const seed of seeds.value) {
    try {
      const r = await submitOneSeed(seed)
      if (r.status === 429) {
        // 限额用尽：立即终止剩余提交
        limitAbortMsg.value = `用户调用限额已用尽，剩余 ${seeds.value.length - submittedSeeds.value} 条种子未提交`
        for (const rest of seeds.value.slice(submittedSeeds.value)) {
          batch.value.submit_failures.push({ seed_type: rest.seed_type, seed_value: rest.seed_value, error: '用户调用限额已用尽' })
        }
        submittedSeeds.value = seeds.value.length
        break
      }
      if (r.tasks) {
        for (const t of r.tasks) {
          batch.value.tasks.push({ task_id: t.task_id, platform: t.platform, seed_type: seed.seed_type, seed_value: seed.seed_value })
        }
      } else {
        batch.value.submit_failures.push({ seed_type: seed.seed_type, seed_value: seed.seed_value, error: r.error })
      }
    } catch (e) {
      batch.value.submit_failures.push({ seed_type: seed.seed_type, seed_value: seed.seed_value, error: e.message })
    }
    submittedSeeds.value++
    saveBatch(batch.value)  // 逐条落盘：关弹窗/刷新后可恢复跟踪
    await sleep(200)
  }

  batch.value.submit_done = true
  saveBatch(batch.value)
  submitting.value = false
  step.value = 'track'
  taskDetails.value = {}
  startTrackPolling()
}

// ─── 跟踪区 ──────────────────────────────────────────────────────────────────
const taskDetails = ref({})  // task_id → GET /ti/tasks/{id} 响应
let trackTimer = null

const TERMINAL = ['done', 'failed']
const isTerminal = (s) => TERMINAL.includes(s)
const finishedCount = computed(() => batch.value ? batch.value.tasks.filter(t => isTerminal(taskDetails.value[t.task_id]?.status)).length : 0)
const failedCount = computed(() => batch.value ? batch.value.tasks.filter(t => taskDetails.value[t.task_id]?.status === 'failed').length : 0)
const allFinished = computed(() => !!batch.value && batch.value.tasks.length > 0 && finishedCount.value === batch.value.tasks.length)
// 跟踪结束展示（含 0 任务的批次：pollTrack 会直接标 finished）
const trackDone = computed(() => !!batch.value && (batch.value.finished || allFinished.value))

function taskStatusLabel(s) {
  return { pending: '排队中', running: '查询中', done: 'done', failed: 'failed' }[s] || s || '查询中'
}

function startTrackPolling() {
  stopTrackPolling()
  pollTrack()
}
function stopTrackPolling() {
  if (trackTimer) { clearTimeout(trackTimer); trackTimer = null }
}

async function pollTrack() {
  if (!batch.value) return
  await Promise.all(batch.value.tasks
    .filter(t => !isTerminal(taskDetails.value[t.task_id]?.status))
    .map(async t => {
      try {
        const res = await authFetch(`/ti/tasks/${t.task_id}`)
        if (res.ok) taskDetails.value = { ...taskDetails.value, [t.task_id]: await res.json() }
        // 任务被删除（404）视为终态失败，否则批次永远跑不完
        else if (res.status === 404) taskDetails.value = { ...taskDetails.value, [t.task_id]: { status: 'failed', error_msg: '任务已被删除' } }
      } catch { /* 网络异常下一轮再试 */ }
    }))
  if (!batch.value.tasks.length || allFinished.value) {
    // 全部终态（或全部提交失败无任务）：仅标记完成，导出到「任务组」手动进行
    batch.value.finished = true
    saveBatch(batch.value)
    return
  }
  trackTimer = setTimeout(pollTrack, 5000)
}

// ─── 任务组 tab ──────────────────────────────────────────────────────────────
const groups = ref([])             // listBatchesSorted() 快照
const groupDetails = ref({})       // task_id → 任务状态（任务组 tab 的轮询结果）
const groupBusy = ref({})          // batch_id → 导出中
const groupErrors = ref({})        // batch_id → 导出错误红字
let groupPollTimer = null

function refreshGroups() {
  groups.value = listBatchesSorted()
}

const groupSeedCount = (b) => new Set(b.tasks.map(t => `${t.seed_type}|${t.seed_value}`)).size
const groupFinishedCount = (b) => b.tasks.filter(t => isTerminal(groupDetails.value[t.task_id]?.status)).length
const groupFailedCount = (b) => b.tasks.filter(t => groupDetails.value[t.task_id]?.status === 'failed').length

function fmtTime(ts) {
  return ts ? new Date(ts).toLocaleString('zh-CN') : '—'
}

function startGroupPolling() {
  stopGroupPolling()
  pollGroups()
}
function stopGroupPolling() {
  if (groupPollTimer) { clearTimeout(groupPollTimer); groupPollTimer = null }
}

async function pollGroups() {
  refreshGroups()
  // 只跟踪未完结且提交已完成的批次（提交中的批次由提交流程自己落盘，避免误判 finished）
  const pending = groups.value.filter(b => !b.finished && b.submit_done !== false)
  for (const g of pending) {
    await Promise.all(g.tasks
      .filter(t => !isTerminal(groupDetails.value[t.task_id]?.status))
      .map(async t => {
        try {
          const res = await authFetch(`/ti/tasks/${t.task_id}`)
          if (res.ok) groupDetails.value = { ...groupDetails.value, [t.task_id]: await res.json() }
          else if (res.status === 404) groupDetails.value = { ...groupDetails.value, [t.task_id]: { status: 'failed', error_msg: '任务已被删除' } }
        } catch { /* 网络异常下一轮再试 */ }
      }))
    if (g.tasks.length && g.tasks.every(t => isTerminal(groupDetails.value[t.task_id]?.status))) {
      g.finished = true
      saveBatch(g)
      refreshGroups()
    }
  }
  // 有未完结批次（含提交中的）才继续轮询
  if (groups.value.some(b => !b.finished)) {
    groupPollTimer = setTimeout(pollGroups, 5000)
  }
}

async function exportGroup(g) {
  groupBusy.value = { ...groupBusy.value, [g.batch_id]: true }
  groupErrors.value = { ...groupErrors.value, [g.batch_id]: '' }
  try {
    await exportBatchSql(g)
  } catch (e) {
    groupErrors.value = { ...groupErrors.value, [g.batch_id]: e.message }
  } finally {
    groupBusy.value = { ...groupBusy.value, [g.batch_id]: false }
  }
}

function deleteGroup(g) {
  if (!confirm(`确定删除任务组 ${g.batch_id} 的记录吗？仅删除本地记录，不影响已提交的任务。`)) return
  removeBatch(g.batch_id)
  refreshGroups()
}

// ─── 弹窗开关 ────────────────────────────────────────────────────────────────
function close() {
  // 不中断提交/跟踪：组件保持挂载（父级用 visible 控制），批次由后台轮询接管
  emit('close')
}

function resetToPick() {
  stopTrackPolling()
  step.value = 'pick'
  batch.value = null
  taskDetails.value = {}
  fileErrors.value = []
  sourceFile.value = ''
  limitAbortMsg.value = ''
}

// 重新打开弹窗时：上一批已跟踪完则回到选择页；进行中则继续展示跟踪；任务组列表恢复轮询
watch(() => props.visible, (v) => {
  if (!v) { stopGroupPolling(); return }
  refreshGroups()
  if (modalTab.value === 'groups') startGroupPolling()
  if (step.value === 'track' && batch.value?.finished) resetToPick()
  if (step.value === 'track' && batch.value && !batch.value.finished && !trackTimer) startTrackPolling()
})

watch(modalTab, (t) => {
  if (t === 'groups') startGroupPolling()
  else stopGroupPolling()
})

onUnmounted(() => { stopTrackPolling(); stopGroupPolling() })
</script>

<template>
  <div v-if="visible" class="modal-mask" @click.self="close">
    <div class="modal batch-modal">
      <div class="modal-header">
        <span class="modal-title">TI 联网解析批量任务</span>
        <button class="modal-close" @click="close" title="关闭不中断已提交的任务">✕</button>
      </div>

      <!-- 顶部 tab：提交任务 / 任务组 -->
      <div class="modal-tabs">
        <button :class="{ active: modalTab === 'submit' }" @click="modalTab = 'submit'">提交任务</button>
        <button :class="{ active: modalTab === 'groups' }" @click="modalTab = 'groups'">任务组</button>
      </div>

      <!-- ═══════ tab：提交任务 ═══════ -->
      <template v-if="modalTab === 'submit'">
        <!-- ── ① 选择文件 ── -->
        <template v-if="step === 'pick'">
          <p class="modal-tip">
            下载模版 JSON，填写种子（IP / 域名 / 样本哈希，最多 100 条）后上传；
            每条种子 × 每个平台产生一个解析任务，进度与 SQL 导出见「任务组」。
          </p>
          <input ref="fileInput" type="file" accept=".json,application/json" style="display:none" @change="onFileChange" />
          <div class="modal-actions">
            <button class="run-btn" @click="pickFile">选择 JSON 文件</button>
            <button class="cancel-btn" @click="downloadTemplate">下载模版</button>
          </div>
          <div v-if="fileErrors.length" class="error-list">
            <div v-for="(err, i) in fileErrors" :key="i" class="error-item">{{ err }}</div>
          </div>
        </template>

        <!-- ── ② 预览与选项 ── -->
        <template v-else-if="step === 'preview'">
          <div class="preview-summary">
            <span class="sum-item">种子总数 <b>{{ seeds.length }}</b></span>
            <span v-for="t in ['ip','domain','file']" :key="t" class="sum-item">
              {{ SEED_TYPE_LABELS[t] }} <b>{{ seedCountByType[t] || 0 }}</b>
            </span>
            <span class="sum-item">将产生任务 <b>{{ totalTaskCount }}</b> 个</span>
          </div>

          <div class="option-row">
            <span class="option-label">目标平台</span>
            <label v-for="p in ALL_PLATFORMS" :key="p" class="plat-check">
              <input type="checkbox" v-model="platforms[p]" /> {{ PLATFORM_LABELS[p] }}
            </label>
          </div>
          <div class="option-row">
            <span class="option-label">缓存复用</span>
            <label class="plat-check"><input type="checkbox" v-model="reuseCache" /> TTL 内同种子同平台直接命中历史结果</label>
          </div>

          <div v-if="queueHints.length" class="queue-hints">
            <span v-for="q in queueHints" :key="q.platform" class="queue-hint">{{ q.name }} 队列 {{ q.used }}/{{ q.max }}</span>
          </div>

          <div class="modal-actions">
            <button class="run-btn" :disabled="!selectedPlatforms.length" @click="submitBatch">提交任务</button>
            <button class="cancel-btn" @click="resetToPick">重新选择</button>
          </div>
        </template>

        <!-- ── ③ 提交中 ── -->
        <template v-else-if="step === 'submitting'">
          <div class="progress-line">
            <span class="spinner"></span> 正在提交：已提交 {{ submittedSeeds }}/{{ seeds.length }}
          </div>
          <p class="modal-tip">关闭弹窗不会中断提交；队列满时自动等待重试（每种子最多 5 次）。</p>
          <div v-if="limitAbortMsg" class="error-list"><div class="error-item">{{ limitAbortMsg }}</div></div>
        </template>

        <!-- ── ④ 跟踪 ── -->
        <template v-else-if="step === 'track' && batch">
          <div class="progress-line" :class="{ done: trackDone }">
            <template v-if="trackDone">✓ 全部完成</template>
            <template v-else><span class="spinner"></span></template>
            已完成 {{ finishedCount }}/{{ batch.tasks.length }}（失败 {{ failedCount }}）
            <span class="batch-id">{{ batch.batch_id }}</span>
          </div>
          <p v-if="trackDone && batch.tasks.length" class="modal-tip">可在「任务组」标签页导出本批 SQL。</p>
          <div v-if="limitAbortMsg" class="error-list"><div class="error-item">{{ limitAbortMsg }}</div></div>

          <div class="task-list">
            <div v-for="t in batch.tasks" :key="t.task_id" class="task-row">
              <span class="plat-badge">{{ PLATFORM_LABELS[t.platform] || t.platform }}</span>
              <span class="task-seed" :title="t.seed_value">{{ t.seed_value }}</span>
              <span :class="['status-badge', taskDetails[t.task_id]?.status || 'running']">
                {{ taskStatusLabel(taskDetails[t.task_id]?.status) }}
              </span>
            </div>
          </div>

          <div v-if="batch.submit_failures.length" class="failure-list">
            <div class="failure-title">提交失败 {{ batch.submit_failures.length }} 条：</div>
            <div v-for="(f, i) in batch.submit_failures" :key="i" class="error-item">
              {{ f.seed_value }}：{{ f.error }}
            </div>
          </div>

          <div class="modal-actions">
            <button class="cancel-btn" @click="resetToPick">再传一批</button>
          </div>
        </template>
      </template>

      <!-- ═══════ tab：任务组 ═══════ -->
      <template v-else>
        <div v-if="!groups.length" class="group-empty">暂无任务组</div>
        <div v-else class="group-list">
          <div v-for="g in groups" :key="g.batch_id" class="group-row">
            <div class="group-main">
              <div class="group-line">
                <span :class="['status-badge', g.finished ? 'done' : 'running']">{{ g.finished ? '已完成' : '进行中' }}</span>
                <span class="group-time">{{ fmtTime(g.created_at) }}</span>
                <span class="group-file" :title="g.source_file">{{ g.source_file || '（无文件名）' }}</span>
              </div>
              <div class="group-line group-meta">
                <span>种子 {{ groupSeedCount(g) }}</span>
                <span>已完成 {{ groupFinishedCount(g) }}/{{ g.tasks.length }}（失败 {{ groupFailedCount(g) }}）</span>
                <span class="batch-id">{{ g.batch_id }}</span>
              </div>
              <div v-if="groupErrors[g.batch_id]" class="error-item">{{ groupErrors[g.batch_id] }}</div>
            </div>
            <div class="group-actions">
              <button class="group-btn export" :disabled="!!groupBusy[g.batch_id]" @click="exportGroup(g)">
                {{ groupBusy[g.batch_id] ? '导出中…' : '导出SQL' }}
              </button>
              <button class="group-btn delete" @click="deleteGroup(g)">删除记录</button>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* ── 弹窗框架（与 TiPlayground 的 modal 一致）── */
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 1000; display: flex; align-items: center; justify-content: center; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; box-shadow: 0 8px 30px rgba(0,0,0,.25); display: flex; flex-direction: column; }
.batch-modal { width: min(680px, 92vw); max-height: 86vh; overflow-y: auto; }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.modal-title { font-size: 15px; font-weight: 600; color: #333; }
.modal-close { background: none; border: none; font-size: 16px; color: #999; cursor: pointer; padding: 2px 6px; }
.modal-close:hover { color: #333; }
.modal-tip { font-size: 12px; color: #888; line-height: 1.6; margin: 0 0 10px; }
.modal-actions { display: flex; gap: 10px; margin-top: 14px; }
.modal-actions .run-btn { flex: 1; }
.run-btn { background: #5470c6; color: #fff; border: none; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; justify-content: center; }
.run-btn:disabled { opacity: .55; cursor: not-allowed; }
.cancel-btn { background: #eceff1; color: #555; border: none; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; }
.spinner { width: 14px; height: 14px; border: 2px solid rgba(84,112,198,.25); border-top-color: #5470c6; border-radius: 50%; animation: spin .7s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── 弹窗内 tab ── */
.modal-tabs { display: flex; gap: 4px; border-bottom: 2px solid #e9ecef; margin-bottom: 14px; }
.modal-tabs button {
  background: transparent; border: none; color: #888; padding: 8px 16px; cursor: pointer;
  font-size: 13.5px; font-weight: 500; border-bottom: 2px solid transparent; margin-bottom: -2px;
}
.modal-tabs button:hover { color: #555; }
.modal-tabs button.active { color: #5470c6; border-bottom-color: #5470c6; }

/* ── 错误列表 ── */
.error-list { margin-top: 10px; max-height: 180px; overflow-y: auto; }
.error-item { color: #c62828; font-size: 12px; line-height: 1.7; }

/* ── 预览区 ── */
.preview-summary { display: flex; flex-wrap: wrap; gap: 14px; background: #f5f7fa; border-radius: 6px; padding: 10px 14px; font-size: 13px; color: #555; margin-bottom: 14px; }
.sum-item b { color: #1a1a2e; }
.option-row { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; font-size: 13px; }
.option-label { font-weight: 600; color: #555; width: 60px; flex-shrink: 0; }
.plat-check { font-size: 13px; color: #444; display: flex; align-items: center; gap: 4px; white-space: nowrap; }
.queue-hints { display: flex; gap: 12px; font-size: 12px; color: #888; }
.queue-hint { background: #f3e5f5; color: #6a3fb5; border-radius: 10px; padding: 2px 10px; }

/* ── 提交 / 跟踪 ── */
.progress-line { display: flex; align-items: center; gap: 8px; font-size: 14px; color: #333; margin-bottom: 10px; flex-wrap: wrap; }
.progress-line.done { color: #2e7d32; font-weight: 600; }
.batch-id { font-size: 11px; color: #aaa; font-family: 'Consolas', 'Monaco', monospace; }
.task-list { border: 1px solid #e8e8e8; border-radius: 6px; max-height: 260px; overflow-y: auto; }
.task-row { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-bottom: 1px solid #f0f2f5; font-size: 13px; }
.task-row:last-child { border-bottom: none; }
.task-seed { flex: 1; color: #333; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; }
.plat-badge { font-size: 12px; color: #6a3fb5; background: #f3e5f5; border-radius: 10px; padding: 1px 10px; flex-shrink: 0; }
.status-badge { font-size: 12px; border-radius: 10px; padding: 1px 10px; background: #eceff1; color: #555; flex-shrink: 0; }
.status-badge.done { background: #e8f5e9; color: #2e7d32; }
.status-badge.failed { background: #ffebee; color: #c62828; }
.status-badge.running { background: #fff3e0; color: #e65100; }
.failure-list { margin-top: 10px; }
.failure-title { font-size: 12px; font-weight: 600; color: #c62828; margin-bottom: 2px; }

/* ── 任务组列表 ── */
.group-empty { text-align: center; padding: 32px 0; color: #aaa; font-size: 13px; }
.group-list { display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
.group-row { display: flex; align-items: center; gap: 10px; border: 1px solid #e8e8e8; border-radius: 6px; padding: 10px 12px; }
.group-main { flex: 1; min-width: 0; }
.group-line { display: flex; align-items: center; gap: 10px; font-size: 13px; color: #333; flex-wrap: wrap; }
.group-meta { margin-top: 4px; font-size: 12px; color: #888; }
.group-time { color: #555; }
.group-file { color: #5470c6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }
.group-actions { display: flex; flex-direction: column; gap: 6px; flex-shrink: 0; }
.group-btn { border: none; border-radius: 6px; padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.group-btn.export { background: #5470c6; color: #fff; }
.group-btn.export:hover:not(:disabled) { background: #455fae; }
.group-btn.export:disabled { opacity: .55; cursor: not-allowed; }
.group-btn.delete { background: #eceff1; color: #888; }
.group-btn.delete:hover { background: #ffebee; color: #c62828; }
</style>
