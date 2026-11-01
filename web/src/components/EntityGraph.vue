<script setup>
import { ref, watch, onMounted, onUnmounted, nextTick, computed } from 'vue'
import * as echarts from 'echarts'
import ChunkOverview from './ChunkOverview.vue'
import EntityDetailModal from './EntityDetailModal.vue'
import TiResultView from './TiResultView.vue'
import { colorFor } from '../utils/chunkColors.js'
import { isAdmin, authFetch, getFeatures } from '../utils/auth.js'

// 功能模块开关：控制类别 tab 的显示与初始类别回退
const features = getFeatures()
// 当前开启的类别列表（全关时兜底 ner，保证组件不崩；此时一般由 graph/iframe 自动探测进入）
const enabledCategories = ['ner', 'attack', 'ti'].filter(c => features[c])
const defaultCategory = enabledCategories[0] || 'ner'

const props = defineProps({
  initialTaskId: { type: String, default: '' },
  isIframe: { type: Boolean, default: false },
  // 导航二级菜单指定的初始类别：ner | attack | ti；空为默认 ner（跳转直达场景由 fetchAutoDetect 修正）
  initialCategory: { type: String, default: '' }
})

// ─── 历史列表状态 ──────────────────────────────────────────────────────────
const historyList    = ref([])
const historyTotal   = ref(0)
const historyPage    = ref(1)
const historySize    = ref(15)  // 改为响应式，默认15
const historySizeOptions = [10, 15, 20, 30]  // 可选的每页条数
const historyLoading = ref(false)
const filterModel    = ref('')
const filterStatus   = ref('')
const availModels    = ref([])
const selectedTasks  = ref(new Set())  // 多选任务 ID
const deleteLoading  = ref(false)

const isAdminUser    = ref(isAdmin())
const users          = ref([])
const selectedUserId = ref('')

async function fetchUsers() {
  if (!isAdminUser.value) return
  try {
    const res = await authFetch('/admin/users?page_size=1000')
    if (res.ok) {
      const data = await res.json()
      users.value = data.items || []
    }
  } catch (e) {
    console.error('获取用户列表失败', e)
  }
}

async function fetchHistory() {
  historyLoading.value = true
  try {
    // 联网情报类别：走 TI 任务列表（字段与 NER 不同，前端按平台/状态过滤）
    if (filterCategory.value === 'ti') {
      const params = new URLSearchParams({ page: historyPage.value, page_size: historySize.value })
      if (filterPlatform.value) params.append('platform', filterPlatform.value)
      if (filterStatus.value)   params.append('status',   filterStatus.value)
      const res  = await authFetch(`/ti/tasks?${params}`)
      const data = await res.json()
      historyList.value  = data.items || []
      historyTotal.value = data.total || 0
      return
    }
    const params = new URLSearchParams({ page: historyPage.value, page_size: historySize.value })
    if (filterModel.value) {
      // 选了具体模型：精确匹配
      params.append('model_name', filterModel.value)
    } else {
      // 未选具体模型：按类别过滤（传入该类别下所有模型名）
      const names = modelsInCategory.value.map(m => m.name)
      if (names.length > 0) {
        names.forEach(n => params.append('model_names', n))
      }
    }
    if (filterStatus.value) params.append('status',   filterStatus.value)
    if (selectedUserId.value) params.append('user_id', selectedUserId.value)
    const res  = await authFetch(`/tasks?${params}`)
    const data = await res.json()
    historyList.value  = data.items || []
    historyTotal.value = data.total || 0
  } catch (e) {
    console.error('获取历史列表失败', e)
  } finally {
    historyLoading.value = false
  }
}


async function fetchModels() {
  try {
    const res  = await authFetch('/models')
    const data = await res.json()
    // 尽量使用 models_info（含 type），没有则回退到普通列表
    if (data.models_info) {
      availModels.value = data.models_info  // [{ name, type }]
    } else {
      availModels.value = (data.models || []).map(n => ({ name: n, type: 'ner' }))
    }
  } catch {}
}

// 第一级类别选择：ner | attack | ti（联网情报）；初始值可由导航二级菜单指定
// 指定的类别被功能开关禁用时，落到第一个可用类别
const filterCategory = ref(
  enabledCategories.includes(props.initialCategory) ? props.initialCategory : defaultCategory
)
// TI 类别的平台过滤（threatbook / qianxin，空为全部）
const filterPlatform = ref('')
const TI_PLATFORM_LABELS = { threatbook: '微步在线', qianxin: '奇安信' }
const TI_SEED_TYPE_LABELS = { ip: 'IP', domain: '域名', file: '样本' }

// 当前类别下可选的模型列表
// ner 类别 = 所有非 attack 模型(含 ner / ioc / auto / llm),
// 与任务统计页分组、NER 调用台的模型过滤口径保持一致;
// 否则 ioc-finder / iocextract / auto 的任务会两边不沾、永远查不出来。
const modelsInCategory = computed(() =>
  availModels.value.filter(m =>
    filterCategory.value === 'attack' ? m.type === 'attack' : m.type !== 'attack'
  )
)

// 切换类别时重置模型选择并重新查询
function onCategoryChange(cat) {
  filterCategory.value = cat
  filterModel.value = ''
  filterPlatform.value = ''
  result.value = null
  tiResult.value = null
  error.value = ''
  applyFilter()
}

// 当前查看的任务是否属于 ATT&CK 模型（用于切换标签文案）
const isAttackResult = computed(() => {
  if (!result.value?.model_name) return false
  const m = availModels.value.find(x => x.name === result.value.model_name)
  return m?.type === 'attack'
})

function totalPages() { return Math.max(1, Math.ceil(historyTotal.value / historySize.value)) }

function gotoPage(p) {
  historyPage.value = Math.max(1, Math.min(p, totalPages()))
  fetchHistory()
}

function changeHistorySize(size) {
  historySize.value = size
  historyPage.value = 1  // 重置到第一页
  fetchHistory()
}

function applyFilter() {
  historyPage.value = 1
  selectedTasks.value.clear()
  fetchHistory()
}

function selectTask(id) {
  taskId.value = String(id)
  runQuery()
}

// 按当前类别查询详情（NER/ATT&CK 走 /tasks，联网情报走 /ti/tasks）
function runQuery() {
  if (filterCategory.value === 'ti') fetchTiResult()
  else fetchResult()
}

function toggleSelectTask(id) {
  const taskIdStr = String(id)
  if (selectedTasks.value.has(taskIdStr)) {
    selectedTasks.value.delete(taskIdStr)
  } else {
    selectedTasks.value.add(taskIdStr)
  }
}

function toggleSelectAll() {
  if (selectedTasks.value.size === historyList.value.length) {
    selectedTasks.value.clear()
  } else {
    selectedTasks.value.clear()
    historyList.value.forEach(item => selectedTasks.value.add(String(item.task_id)))
  }
}

async function deleteSelectedTasks() {
  if (selectedTasks.value.size === 0) return
  if (!confirm(`确定要删除选中的 ${selectedTasks.value.size} 个任务吗？`)) return

  deleteLoading.value = true
  try {
    // NER/ATT&CK 走 /tasks?action=delete；联网情报走 DELETE /ti/tasks/{id}
    const isTi = filterCategory.value === 'ti'
    const deletePromises = Array.from(selectedTasks.value).map(id =>
      isTi
        ? authFetch(`/ti/tasks/${id}`, { method: 'DELETE' })
        : authFetch(`/tasks/${id}?action=delete`)
    )
    const results = await Promise.all(deletePromises)
    const failedCount = results.filter(r => !r.ok).length
    if (failedCount > 0) throw new Error(`${failedCount} 个任务删除失败`)
    selectedTasks.value.clear()
    await fetchHistory()
  } catch (e) {
    alert('删除失败: ' + e.message)
  } finally {
    deleteLoading.value = false
  }
}

function shortId(id)  { return id ? String(id).slice(0, 8) + '…' : '' }
function fmtTime(ts)  { if (!ts) return '—'; return new Date(ts).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) }

// ─── TI 数据导出（管理员：按创建日期把库中数据导出为 SQL 备份文件）─────────────
const exportModal = ref(false)
const exportStart = ref('')
const exportEnd   = ref('')
const exportBusy  = ref(false)
const exportError = ref('')

function todayStr() {
  const d = new Date()
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

function openExportModal() {
  exportStart.value = exportStart.value || todayStr()
  exportEnd.value   = exportEnd.value || todayStr()
  exportError.value = ''
  exportModal.value = true
}

async function submitExport() {
  if (!exportStart.value || !exportEnd.value) { exportError.value = '请选择起止日期'; return }
  if (exportStart.value > exportEnd.value)    { exportError.value = '开始日期不能晚于结束日期'; return }
  exportBusy.value  = true
  exportError.value = ''
  try {
    const params = new URLSearchParams({ start_date: exportStart.value, end_date: exportEnd.value })
    const res = await authFetch(`/ti/export?${params}`)
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || `HTTP ${res.status}`)
    }
    const blob = await res.blob()
    const url  = URL.createObjectURL(blob)
    const a    = document.createElement('a')
    a.href     = url
    a.download = `ti_export_${exportStart.value}_${exportEnd.value}.sql`
    a.click()
    URL.revokeObjectURL(url)
    exportModal.value = false
  } catch (e) {
    exportError.value = e.message
  } finally {
    exportBusy.value = false
  }
}
function fmtLatency(ms) { return ms != null ? Number(ms).toFixed(0) + ' ms' : '—' }
function statusClass(s) { return { done:'st-done', failed:'st-failed', pending:'st-pending', running:'st-running' }[s] || 'st-pending' }
function labelList(labels) {
  if (!labels) return '—'
  const arr = Array.isArray(labels) ? labels : Object.values(labels)
  return arr.slice(0, 3).join(', ') + (arr.length > 3 ? ` +${arr.length - 3}` : '')
}

// ─── 详情状态 ─────────────────────────────────────────────────────────────
const taskId      = ref(props.initialTaskId)
const loading     = ref(false)
const error       = ref('')
const result      = ref(null)
const tiResult    = ref(null)  // 联网情报任务详情（filterCategory === 'ti' 时使用）
let tiPollTimer   = null
const pieEl       = ref(null)
const graphEl     = ref(null)
const graphBoxEl  = ref(null)
const graphFullscreen = ref(false)
let pieChart      = null
let graphChart    = null

// ─── 分块相关状态 (long-text-chunking-ner) ────────────────────────────────
const activeChunkIndex = ref(null)  // 实体 ↔ chunk 双向联动的高亮索引
const pulseEntityKey   = ref(null)  // 点击实体时原文高亮脉冲,1.5s 后自动复位
let pulseTimer = null

// ─── 标签过滤状态 ──────────────────────────────────────────────────────────
// 点击"提取标签"中的标签或饼图某一块 → 只显示该类型实体。再次点击同一个取消过滤。
const activeLabel = ref(null)

const chunkMetadata = computed(() => result.value?.chunk_metadata ?? null)
const chunks = computed(() => chunkMetadata.value?.chunks ?? [])

// 活跃 Chunk 的字符范围(用于原文分段高亮)
const activeChunkRange = computed(() => {
  if (activeChunkIndex.value == null) return null
  const ch = chunks.value.find(c => c.chunk_index === activeChunkIndex.value)
  if (!ch) return null
  return { start: ch.char_start, end: ch.char_end }
})

// 把原文切成 [before, highlighted, after] 三段,用于 v-html 渲染高亮
const inputTextSegments = computed(() => {
  const txt = result.value?.input_text || ''
  const range = activeChunkRange.value
  if (!txt) return null
  if (!range) return { before: txt, highlighted: '', after: '' }
  return {
    before:      txt.slice(0, range.start),
    highlighted: txt.slice(range.start, range.end),
    after:       txt.slice(range.end),
  }
})

function onEntityClick(entity, key) {
  // 点击实体 → 定位到其所属 Chunk 并脉冲原文
  if (entity?.chunk_index != null) {
    activeChunkIndex.value = entity.chunk_index
  }
  pulseEntityKey.value = key
  if (pulseTimer) clearTimeout(pulseTimer)
  pulseTimer = setTimeout(() => { pulseEntityKey.value = null }, 1500)
}

function onChunkRowClick(chunkIndex) {
  // 再次点击同一个 Chunk 行 → 取消选中,恢复原文 / 实体列表的默认视图
  if (activeChunkIndex.value === chunkIndex) {
    activeChunkIndex.value = null
    return
  }

  // 点击 Chunk 行 → 反向高亮原文 chip,并自动翻到包含该 Chunk 第一个实体的分页
  activeChunkIndex.value = chunkIndex

  // 找到该 Chunk 的第一个实体所在全局索引,计算它在第几页,自动翻过去
  const entities = result.value?.entities || []
  const firstIdx = entities.findIndex(e => e.chunk_index === chunkIndex)
  if (firstIdx >= 0) {
    const targetPage = Math.floor(firstIdx / entityPageSize.value) + 1
    if (entityPage.value !== targetPage) {
      entityPage.value = targetPage
    }
    // 滚动到该实体行
    nextTick(() => {
      const el = document.querySelector(`[data-entity-index="${firstIdx}"]`)
      if (el?.scrollIntoView) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }
    })
  }
}

function entityChunkTitle(e) {
  if (e.chunk_index == null) return `${e.label} · ${(e.score * 100).toFixed(0)}%`
  return `所属块 #${e.chunk_index} [${e.chunk_char_start}, ${e.chunk_char_end})\n` +
         `${e.label} · ${(e.score * 100).toFixed(0)}%`
}

// ─── 实体详情弹框 (long-text-chunking-ner) ──────────────────────────────
const modalEntity      = ref(null)
const modalGlobalIndex = ref(-1)

function openEntityDetail(entity, globalIndex, evt) {
  evt?.stopPropagation?.()
  modalEntity.value = entity
  modalGlobalIndex.value = globalIndex
}

function closeEntityDetail() {
  modalEntity.value = null
  modalGlobalIndex.value = -1
}

// ─── 实体列表分页 ─────────────────────────────────────────────────────────
const entityPage = ref(1)
const entityPageSize = ref(50)  // 默认50条/页
const entityPageSizeOptions = [10, 50, 100]

// 根据 activeLabel 过滤后的实体列表(过滤时比较 label,即映射后的标准标签)。
// 与原始 labels 字段联动:点击"提取标签"里的某个标签时,过滤出 label === 该标签 的实体。
const filteredEntities = computed(() => {
  const all = result.value?.entities || []
  if (!activeLabel.value) return all
  return all.filter(e => e.label === activeLabel.value || e.raw_label === activeLabel.value)
})

// 计算分页后的实体列表 (基于 filteredEntities)
const paginatedEntities = computed(() => {
  const start = (entityPage.value - 1) * entityPageSize.value
  const end = start + entityPageSize.value
  return filteredEntities.value.slice(start, end)
})

// 实体总页数
const entityTotalPages = computed(() => {
  return Math.max(1, Math.ceil(filteredEntities.value.length / entityPageSize.value))
})

// "提取标签"区域显示的标签列表 (来自 result.labels,兼容数组/对象两种形态)
const displayedLabels = computed(() => {
  const lbs = result.value?.labels
  if (!lbs) return []
  return Array.isArray(lbs) ? lbs : Object.keys(lbs)
})

// 点击"提取标签"或饼图某项 → 切换 activeLabel
function onLabelClick(label) {
  activeLabel.value = activeLabel.value === label ? null : label
  entityPage.value = 1  // 重置分页到第 1 页
}

// 切换页码
function gotoEntityPage(p) {
  entityPage.value = Math.max(1, Math.min(p, entityTotalPages.value))
}

// 切换每页条数
function changePageSize(size) {
  entityPageSize.value = size
  entityPage.value = 1  // 重置到第一页
}

// 重置分页
function resetEntityPagination() {
  entityPage.value = 1
  entityPageSize.value = 50
}

function toggleFullscreen() {
  const el = graphBoxEl.value
  if (!el) return
  if (!document.fullscreenElement) {
    el.requestFullscreen().catch(err => console.warn('全屏失败', err))
  } else {
    document.exitFullscreen()
  }
}

function onFullscreenChange() {
  graphFullscreen.value = !!document.fullscreenElement
  // 全屏切换后让 ECharts 重绘
  setTimeout(() => graphChart?.resize(), 100)
}

// ─── 查询结果 ──────────────────────────────────────────────────────────────
function destroyCharts() {
  if (pieChart)   { pieChart.dispose();   pieChart   = null }
  if (graphChart) { graphChart.dispose(); graphChart = null }
}

function openIframePage() {
  const id = taskId.value.trim()
  if (!id) return
  window.open(`/iframe/${id}`, '_blank')
}

async function fetchResult() {
  const id = taskId.value.trim()
  if (!id) return
  loading.value = true
  error.value   = ''
  result.value  = null
  tiResult.value = null
  if (tiPollTimer) { clearTimeout(tiPollTimer); tiPollTimer = null }
  destroyCharts()
  resetEntityPagination()  // 重置分页
  activeChunkIndex.value = null
  activeLabel.value = null
  pulseEntityKey.value = null
  if (pulseTimer) { clearTimeout(pulseTimer); pulseTimer = null }
  try {
    const res = await authFetch(`/tasks/${id}/result`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    console.log('Fetched result:', data)  // 调试日志
    if (data.status !== 1) {
      error.value = `${data.msg || '任务未完成'}`
    } else {
      result.value = data
      await nextTick()
      renderCharts(data)
    }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

// ─── 联网情报任务详情（解析详情第三类别）────────────────────────────────────
//  pending/running 时自动轮询直至终态；渲染交给 TiResultView（平台原生风格）。
async function fetchTiResult() {
  const id = taskId.value.trim()
  if (!id) return
  loading.value = true
  error.value   = ''
  result.value  = null
  destroyCharts()
  if (tiPollTimer) { clearTimeout(tiPollTimer); tiPollTimer = null }
  try {
    const res = await authFetch(`/ti/tasks/${id}`)
    if (!res.ok) {
      if (res.status === 404) throw new Error('任务不存在')
      throw new Error(`HTTP ${res.status}`)
    }
    const data = await res.json()
    tiResult.value = data
    if (data.status === 'pending' || data.status === 'running') {
      tiPollTimer = setTimeout(fetchTiResult, 2500)
    }
  } catch (e) {
    tiResult.value = null
    error.value = e.message
  } finally {
    loading.value = false
  }
}

// ─── 渲染图表 ──────────────────────────────────────────────────────────────
function renderCharts(data) {
  renderPie(data.entity_stats)
  renderGraph(filteredEntities.value, data.label_mapping)
}

function renderPie(stats) {
  if (!pieEl.value) return
  if (!pieChart) {
    pieChart = echarts.init(pieEl.value)
    // 饼图点击 → 等同"提取标签"点击
    pieChart.on('click', (p) => {
      if (p?.data?.name) onLabelClick(p.data.name)
    })
  }
  const entries = Object.entries(stats || {})
  if (!entries.length) { pieChart.clear(); return }
  pieChart.setOption({
    title: { text: '实体类型分布', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { orient: 'vertical', right: 10, top: 'center' },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      center: ['40%', '55%'],
      selectedMode: 'single',
      data: entries.map(([name, value]) => ({
        name,
        value,
        // 激活项加描边高亮;非激活项在 activeLabel 存在时整体降透
        itemStyle: activeLabel.value
          ? (name === activeLabel.value
              ? { borderColor: '#111827', borderWidth: 2, opacity: 1 }
              : { opacity: 0.28 })
          : {},
      })),
      label: { show: false },
      emphasis: { label: { show: true, fontSize: 13, fontWeight: 'bold' } },
    }],
  }, /* notMerge */ true)
}

function renderGraph(entities, labelMapping) {
  if (!graphEl.value) return
  if (!graphChart) graphChart = echarts.init(graphEl.value)
  if (!entities?.length) { graphChart.clear(); return }

  // 实体类型色彩表
  const COLORS = [
    '#5470c6','#91cc75','#fac858','#ee6666','#73c0de',
    '#3ba272','#fc8452','#9a60b4','#ea7ccc','#d48265',
  ]
  const labelSet = [...new Set(entities.map(e => e.label))]
  const colorMap = Object.fromEntries(labelSet.map((l, i) => [l, COLORS[i % COLORS.length]]))

  // 节点：每个实体一个节点
  const nodes = entities.map((e, i) => ({
    id:         String(i),
    name:       e.text,
    value:      e.score,
    category:   labelSet.indexOf(e.label),
    symbolSize: 20 + e.score * 20,
    label:      { show: true, fontSize: 11 },
    itemStyle:  { color: colorMap[e.label] },
    tooltip:    `${e.text}\n类型: ${e.label}\n置信度: ${(e.score * 100).toFixed(0)}%`,
  }))

  // 同类型实体之间连边（最多连 5 条，避免过密）
  const edges = []
  const byLabel = {}
  entities.forEach((e, i) => {
    if (!byLabel[e.label]) byLabel[e.label] = []
    byLabel[e.label].push(i)
  })
  for (const group of Object.values(byLabel)) {
    for (let i = 0; i < group.length - 1 && i < 5; i++) {
      edges.push({ source: String(group[i]), target: String(group[i + 1]), lineStyle: { opacity: 0.4 } })
    }
  }

  const categories = labelSet.map(name => ({ name, itemStyle: { color: colorMap[name] } }))

  graphChart.setOption({
    title: {
      text: activeLabel.value
        ? `实体关系图 · 筛选 ${activeLabel.value}(${entities.length})`
        : '实体关系图',
      left: 'center',
      textStyle: { fontSize: 14 },
    },
    tooltip: {
      trigger: 'item',
      formatter: (p) => p.data.tooltip || p.name,
    },
    legend: [{
      data: categories.map(c => c.name),
      orient: 'vertical',
      right: 10,
      top: 'center',
      textStyle: { fontSize: 12 },
    }],
    series: [{
      type: 'graph',
      layout: 'force',
      nodes,
      edges,
      categories,
      roam: true,
      draggable: true,
      force: { repulsion: 120, edgeLength: [60, 150] },
      lineStyle: { color: '#ccc', curveness: 0.1 },
      emphasis: { focus: 'adjacency' },
    }],
  }, /* notMerge */ true)
}

// 外部注入 task_id 时自动查询（先按 NER 任务探测，404 则按联网情报任务处理——
// 联网情报调用台「查看情报展示」跳转入口走这里）
async function fetchAutoDetect() {
  const val = taskId.value?.trim()
  if (!val) return
  try {
    const res = await authFetch(`/tasks/${val}/result`)
    if (res.status === 404 && features.ti) {
      filterCategory.value = 'ti'
      fetchTiResult()
      return
    }
  } catch { /* 网络异常时按默认 NER 流程走，错误在 fetchResult 里呈现 */ }
  fetchResult()
}
watch(() => props.initialTaskId, (val) => {
  if (val) { taskId.value = val; fetchAutoDetect() }
})

// activeLabel 变化 → 重新渲染饼图描边 + 关系图过滤
watch(activeLabel, () => {
  if (!result.value) return
  renderPie(result.value.entity_stats)
  renderGraph(filteredEntities.value, result.value.label_mapping)
})

// ─── 生命周期 ──────────────────────────────────────────────────────────────
onMounted(async () => {
  fetchModels()
  if (isAdminUser.value) {
    await fetchUsers()
  }
  if (!props.isIframe) {
    fetchHistory()
  }
  if (props.initialTaskId) fetchAutoDetect()
  window.addEventListener('resize', handleResize)
  document.addEventListener('fullscreenchange', onFullscreenChange)
})
onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  if (tiPollTimer) clearTimeout(tiPollTimer)
  pieChart?.dispose()
  graphChart?.dispose()
})
function handleResize() {
  pieChart?.resize()
  graphChart?.resize()
}
</script>

<template>
  <div class="eg-root">

    <!-- ═══ 左侧：历史列表 ═══ -->
    <aside class="history-panel" v-if="!isIframe">
      <div class="history-header">
        <span class="history-title">历史任务</span>
        <div class="header-actions">
          <button
            v-if="filterCategory === 'ti' && isAdminUser"
            class="export-btn"
            @click="openExportModal"
            title="按创建日期导出联网情报库数据为 SQL 文件（可导入另一套数据库）"
          >
            ⤓ 导出
          </button>
          <button 
            v-if="selectedTasks.size > 0" 
            class="delete-btn" 
            @click="deleteSelectedTasks"
            :disabled="deleteLoading"
            title="删除选中"
          >
            🗑️ {{ deleteLoading ? '删除中...' : `删除(${selectedTasks.size})` }}
          </button>
          <button class="refresh-btn" @click="applyFilter" title="刷新">↺</button>
        </div>
      </div>

      <!-- 第一级：类别切换 Tab（独立一行，按功能开关过滤） -->
      <div class="filter-category-row">
        <div class="category-tabs">
          <button
            v-if="features.ner"
            :class="{ 'cat-active': filterCategory === 'ner' }"
            @click="onCategoryChange('ner')"
          >NER</button>
          <button
            v-if="features.attack"
            :class="{ 'cat-active': filterCategory === 'attack' }"
            @click="onCategoryChange('attack')"
          >ATT&amp;CK</button>
          <button
            v-if="features.ti"
            :class="{ 'cat-active': filterCategory === 'ti' }"
            @click="onCategoryChange('ti')"
          >联网情报</button>
        </div>
      </div>

      <!-- 第二级：用户 + 模型/平台 + 状态（独立一行） -->
      <div class="filter-row">
        <select v-if="isAdminUser && filterCategory !== 'ti'" v-model="selectedUserId" @change="applyFilter">
          <option value="">全部用户</option>
          <option v-for="u in users" :key="u.id" :value="u.id">{{ u.username }}</option>
        </select>
        <select v-if="filterCategory === 'ti'" v-model="filterPlatform" @change="applyFilter">
          <option value="">全部平台</option>
          <option value="threatbook">微步在线</option>
          <option value="qianxin">奇安信</option>
        </select>
        <select v-else v-model="filterModel" @change="applyFilter">
          <option value="">全部模型</option>
          <option v-for="m in modelsInCategory" :key="m.name" :value="m.name">{{ m.name }}</option>
        </select>
        <select v-model="filterStatus" @change="applyFilter">
          <option value="">全部状态</option>
          <option value="done">done</option>
          <option value="pending">pending</option>
          <option value="running">running</option>
          <option value="failed">failed</option>
        </select>
      </div>

      <!-- 全选 -->
      <div class="select-all-row" v-if="historyList.length > 0">
        <label class="checkbox-label">
          <input 
            type="checkbox" 
            :checked="selectedTasks.size === historyList.length && historyList.length > 0"
            @change="toggleSelectAll"
          />
          <span>全选</span>
        </label>
      </div>

      <!-- 列表 -->
      <div class="history-list" v-if="!historyLoading">
        <div
          v-for="item in historyList"
          :key="item.task_id"
          class="history-item"
          :class="{ active: String(item.task_id) === taskId }"
        >
          <div class="item-checkbox">
            <input 
              type="checkbox" 
              :checked="selectedTasks.has(String(item.task_id))"
              @click.stop="toggleSelectTask(item.task_id)"
            />
          </div>
          <!-- TI 任务列表项 -->
          <div v-if="filterCategory === 'ti'" class="item-content" @click="selectTask(item.task_id)">
            <div class="item-row1">
              <code class="item-id" :title="String(item.task_id)">{{ shortId(item.task_id) }}</code>
              <span :class="['status-dot', statusClass(item.status)]">{{ item.status }}</span>
            </div>
            <div class="item-row2">
              <span class="item-model">{{ TI_PLATFORM_LABELS[item.platform] || item.platform }}</span>
              <span class="item-latency">{{ fmtLatency(item.latency_ms) }}</span>
            </div>
            <div class="item-row3">
              <span class="item-labels">{{ TI_SEED_TYPE_LABELS[item.seed_type] || item.seed_type }}</span>
            </div>
            <div class="item-row4">
              <span class="item-text" :title="item.seed_value">{{ item.seed_value }}</span>
            </div>
            <div class="item-time">{{ fmtTime(item.created_at) }}</div>
          </div>
          <!-- NER / ATT&CK 任务列表项 -->
          <div v-else class="item-content" @click="selectTask(item.task_id)">
            <div class="item-row1">
              <code class="item-id" :title="String(item.task_id)">{{ shortId(item.task_id) }}</code>
              <span :class="['status-dot', statusClass(item.status)]">{{ item.status }}</span>
            </div>
            <div class="item-row2">
              <span class="item-model">{{ item.model_name }}</span>
              <span class="item-latency">{{ fmtLatency(item.latency_ms) }}</span>
              <span v-if="item.chunk_count" class="item-chunks" title="分块数量">📦 {{ item.chunk_count }}</span>
            </div>
            <div class="item-row3">
              <span class="item-labels" :title="(item.labels || []).join(', ')">{{ labelList(item.labels) }}</span>
            </div>
            <div class="item-row4">
              <span class="item-text" :title="item.input_text_preview">{{ item.input_text_preview }}</span>
            </div>
            <div class="item-time">{{ fmtTime(item.created_at) }}</div>
          </div>
        </div>
        <div v-if="!historyList.length" class="history-empty">暂无记录</div>
      </div>
      <div v-else class="history-loading">加载中…</div>

      <!-- 分页 -->
      <div class="pagination">
        <div class="page-size-row">
          <span class="page-size-label">每页：</span>
          <button 
            v-for="size in historySizeOptions" 
            :key="size"
            :class="['size-btn', { active: historySize === size }]"
            @click="changeHistorySize(size)"
          >
            {{ size }}
          </button>
        </div>
        <div class="page-nav-row">
          <button :disabled="historyPage <= 1"             @click="gotoPage(historyPage - 1)">‹</button>
          <span>{{ historyPage }} / {{ totalPages() }}</span>
          <button :disabled="historyPage >= totalPages()" @click="gotoPage(historyPage + 1)">›</button>
        </div>
        <span class="total-tip">共 {{ historyTotal }} 条</span>
      </div>
    </aside>

    <!-- ═══ 右侧：详情 ═══ -->
    <div :class="['detail-panel', { 'iframe-view': isIframe }]">
      <h2>解析详情</h2>

      <!-- 查询输入 -->
      <div class="query-row" v-if="!isIframe">
        <input v-model="taskId" placeholder="输入 Task ID 或从左侧列表点击选择" @keyup.enter="runQuery" />
        <button :disabled="loading || !taskId.trim()" @click="runQuery">
          {{ loading ? '查询中…' : '查询' }}
        </button>
        <button 
          v-if="taskId.trim()" 
          class="open-iframe-btn" 
          @click="openIframePage"
          title="在新标签页中打开当前任务的 iframe 集成页面"
        >
          打开集成页面 ↗
        </button>
      </div>
      <div v-if="error" class="error-msg">{{ error }}</div>

      <!-- 联网情报详情：按平台原生页面风格展示 -->
      <template v-if="tiResult">
        <TiResultView :task="tiResult" />
      </template>

      <template v-else-if="result">
        <!-- 基本信息 -->
        <div class="meta-bar">
          <span>模型: <strong>{{ result.model_name }}</strong></span>
          <span>延迟: <strong>{{ result.latency_ms?.toFixed(0) }} ms</strong></span>
          <span>{{ isAttackResult ? 'ATT&amp;CK数' : '实体数' }}: <strong>{{ result.entities.length }}</strong></span>
          <span>类型数: <strong>{{ Object.keys(result.entity_stats).length }}</strong></span>
          <span v-if="chunkMetadata">
            分块: <strong>{{ chunkMetadata.chunking_enabled ? chunkMetadata.chunk_count + ' 块' : '未启用' }}</strong>
          </span>
        </div>

        <!-- 分块信息(概览 + Chunk 明细,long-text-chunking-ner Requirement 7.1-7.4) -->
        <ChunkOverview
          :chunk-metadata="chunkMetadata"
          :active-chunk-index="activeChunkIndex"
          @chunk-click="onChunkRowClick"
        />

        <!-- 原始查询信息 -->
        <div class="section">
          <h3>查询信息</h3>
          <div class="query-info">
            <div class="info-item">
              <label>原始文本：</label>
              <div v-if="inputTextSegments" class="info-content text-content">
                <template v-if="activeChunkRange">
                  <span class="raw-before">{{ inputTextSegments.before }}</span><span
                    class="raw-highlight"
                    :title="'所属 Chunk #' + activeChunkIndex + ' [' + activeChunkRange.start + ',' + activeChunkRange.end + ')'"
                  >{{ inputTextSegments.highlighted }}</span><span class="raw-after">{{ inputTextSegments.after }}</span>
                </template>
                <template v-else>{{ result.input_text || '（无数据）' }}</template>
              </div>
              <div v-else class="info-content text-content">（无数据）</div>
            </div>
            <div class="info-item">
              <label>提取标签：</label>
              <div class="info-content">
                <span v-if="displayedLabels.length" class="label-list">
                  <button
                    v-for="(lb, i) in displayedLabels"
                    :key="i"
                    type="button"
                    :class="['label-tag', {
                      'label-tag-active': activeLabel === lb,
                      'label-tag-dimmed': activeLabel && activeLabel !== lb,
                    }]"
                    :title="activeLabel === lb ? '再次点击取消过滤' : '点击仅显示此类实体'"
                    @click="onLabelClick(lb)"
                  >{{ lb }}</button>
                </span>
                <span v-else class="no-data">（无数据）</span>
                <button
                  v-if="activeLabel"
                  type="button"
                  class="clear-filter-btn"
                  @click="onLabelClick(activeLabel)"
                >✕ 清除"{{ activeLabel }}"筛选</button>
              </div>
            </div>
          </div>
        </div>

        <!-- 实体标签 -->
        <div class="section" v-if="result.entities.length">
          <h3>
            {{ isAttackResult ? 'ATT&amp;CK标注' : '实体标注' }}
            <span v-if="activeLabel" class="filter-indicator">
              · 仅显示
              <span class="filter-chip">{{ activeLabel }}</span>
            </span>
          </h3>
          <div class="entity-chips">
            <span
              v-for="(e, i) in result.entities"
              :key="i"
              :class="[
                'chip',
                { pulse: pulseEntityKey === 'chip-' + i,
                  dimmed: (activeChunkIndex != null && e.chunk_index !== activeChunkIndex)
                          || (activeLabel && e.label !== activeLabel && e.raw_label !== activeLabel) },
              ]"
              :style="{ backgroundColor: colorFor(e.chunk_index) }"
              :title="entityChunkTitle(e)"
              @click="openEntityDetail(e, i)"
            >
              {{ e.text }}<em>{{ e.label }}</em>
              <small v-if="e.raw_label && e.raw_label !== e.label">← {{ e.raw_label }}</small>
              <small v-if="e.chunk_index != null" class="chunk-tag">#{{ e.chunk_index }}</small>
            </span>
          </div>
        </div>

        <!-- 图表区 -->
        <div class="charts-row">
          <div class="chart-box"><div ref="pieEl"   class="chart"></div></div>
          <div class="chart-box graph-box" ref="graphBoxEl">
            <button class="fullscreen-btn" @click="toggleFullscreen" :title="graphFullscreen ? '退出全屏' : '全屏查看'">
              <svg v-if="!graphFullscreen" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
              <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
            </button>
            <div ref="graphEl" class="chart"></div>
          </div>
        </div>

        <!-- 详情表格 -->
        <div class="section">
          <div class="section-header">
            <h3>
              {{ isAttackResult ? 'ATT&amp;CK详情' : '实体详情' }}
              <span v-if="activeLabel" class="filter-indicator">
                · 已筛选
                <span class="filter-chip">{{ activeLabel }}</span>
              </span>
            </h3>
            <div class="pagination-controls">
              <span class="total-info">
                共 {{ filteredEntities.length }} 条
                <template v-if="activeLabel">
                  / 全部 {{ result.entities.length }} 条
                </template>
              </span>
              <div class="page-size-selector">
                <span class="page-size-label">每页：</span>
                <button 
                  v-for="size in entityPageSizeOptions" 
                  :key="size"
                  :class="['page-size-btn', { active: entityPageSize === size }]"
                  @click="changePageSize(size)"
                >
                  {{ size }}
                </button>
              </div>
              <div class="page-nav">
                <button :disabled="entityPage <= 1" @click="gotoEntityPage(entityPage - 1)">‹</button>
                <span class="page-info">{{ entityPage }} / {{ entityTotalPages }}</span>
                <button :disabled="entityPage >= entityTotalPages" @click="gotoEntityPage(entityPage + 1)">›</button>
              </div>
            </div>
          </div>
          <table class="entity-table">
            <thead>
              <tr>
                <th>#</th><th>文本</th><th>标准标签</th><th>原始标签</th><th>位置</th><th>置信度</th>
                <th>Chunk</th><th>Chunk 范围</th><th>模型</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(e, i) in paginatedEntities"
                :key="i"
                :data-entity-index="(entityPage - 1) * entityPageSize + i"
                :class="{ 'row-active': activeChunkIndex != null && e.chunk_index === activeChunkIndex }"
                :title="'点击查看 ' + e.text + ' 在原文中的位置'"
                @click="openEntityDetail(e, (entityPage - 1) * entityPageSize + i)"
              >
                <td>{{ (entityPage - 1) * entityPageSize + i + 1 }}</td>
                <td><strong>{{ e.text }}</strong></td>
                <td><span class="label-badge">{{ e.label }}</span></td>
                <td><span v-if="e.raw_label && e.raw_label !== e.label" class="raw-label">{{ e.raw_label }}</span><span v-else>—</span></td>
                <td>{{ e.start }}–{{ e.end }}</td>
                <td>{{ (e.score * 100).toFixed(1) }}%</td>
                <td>
                  <span
                    v-if="e.chunk_index != null"
                    class="chunk-cell"
                    :style="{ backgroundColor: colorFor(e.chunk_index) }"
                  >#{{ e.chunk_index }}</span>
                  <span v-else class="placeholder-dash">—</span>
                </td>
                <td class="mono-small">
                  <template v-if="e.chunk_char_start != null">
                    {{ e.chunk_char_start }}–{{ e.chunk_char_end }}
                  </template>
                  <span v-else class="placeholder-dash">—</span>
                </td>
                <td>
                  <span v-if="e.model_name" class="model-badge">{{ e.model_name }}</span>
                  <span v-else class="placeholder-dash">—</span>
                </td>
              </tr>
              <tr v-if="!result.entities.length">
                <td colspan="9" class="empty">未识别到实体</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <!-- 空状态 -->
      <div v-else-if="!loading && !error" class="empty-hint">
        从左侧列表选择任务，或输入 Task ID 查询
      </div>
    </div>

    <!-- 实体详情弹框 -->
    <EntityDetailModal
      v-if="modalEntity"
      :entity="modalEntity"
      :input-text="result?.input_text || ''"
      :chunks="chunks"
      :all-entities="result?.entities || []"
      :global-index="modalGlobalIndex"
      @close="closeEntityDetail"
      @goto-chunk="onChunkRowClick"
    />

    <!-- TI 数据导出弹窗（按创建日期导出 SQL 备份） -->
    <div v-if="exportModal" class="modal-mask" @click.self="exportModal = false">
      <div class="modal export-modal">
        <div class="modal-header">
          <span class="modal-title">导出联网情报数据</span>
          <button class="modal-close" @click="exportModal = false">✕</button>
        </div>
        <p class="export-tip">
          按任务创建日期导出 <code>ti_tasks</code> / <code>ti_task_results</code> 为 SQL 文件，
          可在另一套 PostgreSQL 中执行 <code>psql -f</code> 加载（幂等，重复导入不产生重复行）。
        </p>
        <div class="export-dates">
          <label>开始日期<input type="date" v-model="exportStart" /></label>
          <label>结束日期<input type="date" v-model="exportEnd" /></label>
        </div>
        <div v-if="exportError" class="export-error">{{ exportError }}</div>
        <div class="modal-actions">
          <button class="export-submit" :disabled="exportBusy" @click="submitExport">
            {{ exportBusy ? '导出中…' : '导出 SQL' }}
          </button>
          <button class="export-cancel" @click="exportModal = false">取消</button>
        </div>
      </div>
    </div>

  </div>
</template>

<style scoped>
/* ── 整体布局 ── */
.eg-root { display: flex; gap: 16px; align-items: flex-start; min-height: 600px; }

/* ── 左侧历史面板 ── */
.history-panel {
  width: 280px; flex-shrink: 0;
  background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.1);
  display: flex; flex-direction: column; overflow: hidden;
  /* 粘性定位:左侧面板固定在视口顶部,不随右侧内容滚动 */
  position: sticky;
  top: 16px;
  align-self: flex-start;
  max-height: calc(100vh - 56px - 32px); /* 100vh - header高度 - 上下padding */
}
.history-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 14px 8px; border-bottom: 1px solid #f0f0f0; }
.history-title  { font-size: 15px; font-weight: 600; color: #333; }
.header-actions { display: flex; gap: 6px; align-items: center; }
.refresh-btn    { background: none; border: none; font-size: 16px; cursor: pointer; color: #888; padding: 2px 6px; border-radius: 4px; }
.refresh-btn:hover { background: #f0f0f0; color: #333; }
.delete-btn {
  background: #ef4444; color: #fff; border: none; border-radius: 4px;
  padding: 4px 10px; font-size: 12px; cursor: pointer; transition: background .2s;
}
.delete-btn:hover { background: #dc2626; }
.delete-btn:disabled { background: #fca5a5; cursor: not-allowed; }
.export-btn {
  background: #fff; color: #5470c6; border: 1px solid #5470c6; border-radius: 4px;
  padding: 3px 10px; font-size: 12px; cursor: pointer; transition: all .2s;
}
.export-btn:hover { background: #f0f4ff; }

/* ── TI 导出弹窗 ── */
.modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 1000; display: flex; align-items: center; justify-content: center; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; box-shadow: 0 8px 30px rgba(0,0,0,.25); display: flex; flex-direction: column; }
.modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.modal-title { font-size: 15px; font-weight: 600; color: #333; }
.modal-close { background: none; border: none; font-size: 16px; color: #999; cursor: pointer; padding: 2px 6px; }
.modal-close:hover { color: #333; }
.export-modal { width: min(480px, 92vw); }
.export-tip { font-size: 12px; color: #888; line-height: 1.7; margin-bottom: 14px; }
.export-tip code { background: #eceff1; padding: 1px 5px; border-radius: 3px; font-size: 11px; color: #c62828; }
.export-dates { display: flex; gap: 12px; }
.export-dates label { flex: 1; display: flex; flex-direction: column; gap: 5px; font-size: 13px; font-weight: 600; color: #555; }
.export-dates input { padding: 8px 10px; border: 1px solid #d0d0d0; border-radius: 6px; font-size: 13px; outline: none; }
.export-dates input:focus { border-color: #5470c6; }
.export-error { margin-top: 10px; color: #c62828; font-size: 13px; }
.modal-actions { display: flex; gap: 10px; margin-top: 16px; }
.export-submit { flex: 1; background: #5470c6; color: #fff; border: none; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; }
.export-submit:disabled { opacity: .55; cursor: not-allowed; }
.export-cancel { background: #eceff1; color: #555; border: none; border-radius: 6px; padding: 9px 18px; font-size: 14px; cursor: pointer; }

.filter-row { display: flex; gap: 6px; padding: 8px 10px; background: #fafafa; border-bottom: 1px solid #f0f0f0; align-items: center; flex-wrap: wrap; }
.filter-row select { flex: 1; min-width: 0; text-overflow: ellipsis; border: 1px solid #e0e0e0; border-radius: 4px; padding: 4px 2px; font-size: 12px; background: #fff; outline: none; }

/* 二级过滤：类别 Tab */
/* 类别切换行（NER / ATT&CK Tab 独占一行） */
.filter-category-row {
  display: flex;
  align-items: center;
  padding: 8px 10px 4px;
  background: #fafafa;
  border-bottom: 1px solid #f0f0f0;
}

.category-tabs {

  display: flex;
  gap: 0;
  border: 1px solid #e0e0e0;
  border-radius: 4px;
  overflow: hidden;
  flex-shrink: 0;
}
.category-tabs button {
  background: #fff;
  border: none;
  padding: 3px 10px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  color: #888;
  transition: all .15s;
  white-space: nowrap;
}
.category-tabs button:hover { background: #f0f0f0; color: #333; }
.category-tabs button.cat-active { background: #1a1a2e; color: #fff; }

.select-all-row { padding: 8px 12px; background: #f8f9fa; border-bottom: 1px solid #f0f0f0; }
.checkbox-label { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #555; cursor: pointer; user-select: none; }
.checkbox-label input[type="checkbox"] { cursor: pointer; }

.history-list { flex: 1; overflow-y: auto; }
.history-item {
  display: flex; gap: 8px; align-items: flex-start;
  padding: 10px 12px; border-bottom: 1px solid #f5f5f5;
  transition: background .1s;
}
.history-item:hover  { background: #f8f9ff; }
.history-item.active { background: #eef2ff; border-left: 3px solid #5470c6; }
.item-checkbox { padding-top: 2px; }
.item-checkbox input[type="checkbox"] { cursor: pointer; }
.item-content { flex: 1; cursor: pointer; }
.item-row1 { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.item-id   { font-size: 11px; color: #5470c6; font-family: monospace; }
.item-row2 { display: flex; justify-content: space-between; margin-bottom: 3px; }
.item-model   { font-size: 12px; color: #555; font-weight: 600; }
.item-latency { font-size: 11px; color: #888; }
.item-row3 { margin-bottom: 3px; }
.item-labels { font-size: 11px; color: #7c3aed; background: #f3f0ff; padding: 1px 6px; border-radius: 10px; }
.item-row4 { margin-bottom: 3px; }
.item-text { font-size: 11px; color: #666; display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; }
.item-time { font-size: 10px; color: #bbb; text-align: right; }

/* 状态点 */
.status-dot { font-size: 10px; padding: 1px 6px; border-radius: 8px; font-weight: 600; }
.st-done    { background: #d1fae5; color: #065f46; }
.st-failed  { background: #fee2e2; color: #991b1b; }
.st-pending { background: #fef3c7; color: #92400e; }
.st-running { background: #dbeafe; color: #1e40af; }

.history-empty   { text-align: center; color: #bbb; font-size: 13px; padding: 32px; }
.history-loading { text-align: center; color: #bbb; font-size: 13px; padding: 32px; }

/* 分页 */
.pagination { 
  display: flex; 
  flex-direction: column;
  gap: 8px; 
  padding: 10px 12px; 
  border-top: 1px solid #f0f0f0; 
  background: #fafafa;
}
.page-size-row {
  display: flex;
  align-items: center;
  gap: 6px;
  justify-content: center;
}
.page-size-label {
  font-size: 12px;
  color: #666;
}
.size-btn {
  background: #fff;
  border: 1px solid #e0e0e0;
  border-radius: 4px;
  padding: 3px 10px;
  cursor: pointer;
  font-size: 12px;
  color: #555;
  transition: all 0.2s;
}
.size-btn:hover {
  background: #f0f0f0;
  border-color: #d0d0d0;
}
.size-btn.active {
  background: #5470c6;
  border-color: #5470c6;
  color: #fff;
  font-weight: 600;
}
.page-nav-row {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: center;
}
.page-nav-row button { 
  background: #fff; 
  border: 1px solid #e0e0e0; 
  border-radius: 4px; 
  padding: 3px 10px; 
  cursor: pointer; 
  font-size: 14px; 
}
.page-nav-row button:disabled { 
  opacity: .4; 
  cursor: not-allowed; 
}
.page-nav-row span  { 
  font-size: 12px; 
  color: #555; 
}
.total-tip { 
  color: #bbb !important; 
  font-size: 11px;
  text-align: center;
}

/* ── 右侧详情 ── */
.detail-panel { flex: 1; min-width: 0; background: #fff; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
.detail-panel.iframe-view { border-radius: 0; box-shadow: none; border: none; padding: 16px 8px; }
h2  { font-size: 18px; margin-bottom: 16px; }
h3  { font-size: 15px; margin-bottom: 10px; color: #444; }
.section { margin-bottom: 24px; }

.query-row { display: flex; gap: 10px; margin-bottom: 16px; }
.query-row input  { flex: 1; border: 1px solid #ddd; border-radius: 6px; padding: 8px 12px; font-size: 14px; outline: none; }
.query-row input:focus { border-color: #5470c6; }
.query-row button { padding: 8px 20px; background: #5470c6; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; }
.query-row button.open-iframe-btn { background: #fff; color: #5470c6; border: 1px solid #5470c6; font-weight: 500; }
.query-row button.open-iframe-btn:hover { background: #f0f4ff; }
.query-row button:disabled { background: #ccc; cursor: not-allowed; }
.error-msg { color: #e74c3c; background: #fee2e2; padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 13px; }

.meta-bar { display: flex; gap: 24px; padding: 12px 16px; background: #f8f9ff; border-radius: 6px; margin-bottom: 20px; font-size: 14px; color: #555; }
.meta-bar strong { color: #333; }

.query-info { background: #fafafa; border: 1px solid #e8e8e8; border-radius: 6px; padding: 16px; }
.info-item { margin-bottom: 12px; }
.info-item:last-child { margin-bottom: 0; }
.info-item label { font-size: 13px; font-weight: 600; color: #555; display: block; margin-bottom: 6px; }
.info-content { font-size: 13px; color: #333; line-height: 1.6; }
.text-content { background: #fff; border: 1px solid #e0e0e0; border-radius: 4px; padding: 10px 12px; white-space: pre-wrap; word-break: break-word; max-height: 400px; overflow-y: auto; }
.raw-before, .raw-after { color: #9ca3af; }
.raw-highlight {
  background: linear-gradient(transparent 55%, #fde68a 55%);
  color: #111827;
  font-weight: 500;
  padding: 0 1px;
  border-radius: 2px;
  box-shadow: 0 0 0 1px rgba(234, 179, 8, .3);
}
.label-list { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.label-tag {
  background: #e0e7ff;
  color: #3730a3;
  padding: 3px 12px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid transparent;
  cursor: pointer;
  transition: all .15s;
  font-family: inherit;
}
.label-tag:hover {
  background: #c7d2fe;
  transform: translateY(-1px);
}
.label-tag-active {
  background: #3730a3;
  color: #fff;
  border-color: #1e1b4b;
  box-shadow: 0 2px 6px rgba(55, 48, 163, .35);
}
.label-tag-active:hover {
  background: #1e1b4b;
}
.label-tag-dimmed {
  opacity: 0.42;
}
.label-tag-dimmed:hover {
  opacity: 0.7;
}
.clear-filter-btn {
  margin-left: 8px;
  background: #fff;
  color: #6b7280;
  border: 1px solid #d1d5db;
  border-radius: 12px;
  padding: 3px 10px;
  font-size: 11px;
  cursor: pointer;
  transition: all .15s;
}
.clear-filter-btn:hover {
  background: #f3f4f6;
  color: #dc2626;
  border-color: #dc2626;
}

/* 过滤状态指示(出现在 h3 标题旁) */
.filter-indicator {
  font-size: 13px;
  font-weight: 400;
  color: #6b7280;
  margin-left: 8px;
}
.filter-chip {
  display: inline-block;
  padding: 1px 8px;
  background: #3730a3;
  color: #fff;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  margin-left: 4px;
  vertical-align: baseline;
}
.no-data { color: #999; font-style: italic; }

.entity-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  background: #eef2ff;
  border: 1px solid #c7d2fe;
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 13px;
  cursor: pointer;
  transition: opacity .15s, transform .15s, box-shadow .15s;
}
.chip:hover { box-shadow: 0 1px 4px rgba(0, 0, 0, .12); transform: translateY(-1px); }
.chip em    { font-style: normal; color: #5470c6; font-weight: 600; margin-left: 4px; }
.chip small { color: #aaa; font-size: 11px; margin-left: 4px; }
.chip small.chunk-tag {
  background: rgba(84, 112, 198, .18);
  color: #3556a3;
  border-radius: 10px;
  padding: 1px 6px;
  font-weight: 600;
}
.chip.dimmed { opacity: .35; }
.chip.pulse {
  animation: chip-pulse 1.5s ease-out 1;
  box-shadow: 0 0 0 3px rgba(84, 112, 198, .35);
}
@keyframes chip-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(84, 112, 198, .55); }
  70%  { box-shadow: 0 0 0 8px rgba(84, 112, 198, 0); }
  100% { box-shadow: 0 0 0 0 rgba(84, 112, 198, 0); }
}

/* 实体表新增列样式 */
.entity-table tr.row-active td { background: #eef4ff; box-shadow: inset 3px 0 0 #5470c6; }
.entity-table tbody tr {
  cursor: pointer;
  transition: background-color .12s;
}
.entity-table tbody tr:hover td {
  background: #f4f6ff;
}
.entity-table tbody tr.row-active:hover td { background: #e4ecff; }
.chunk-cell {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 36px;
  padding: 2px 8px;
  border-radius: 10px;
  color: #111827;
  font-weight: 600;
  font-size: 12px;
  border: 1px solid rgba(0, 0, 0, .06);
}
.placeholder-dash { color: #bbb; font-size: 13px; }
.model-badge {
  display: inline-block;
  padding: 1px 7px;
  background: #f0f4ff;
  color: #3730a3;
  border: 1px solid #c7d2fe;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  vertical-align: middle;
}
.mono-small {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
  color: #6b7280;
}

/* 历史列表 chunk_count 徽标 */
.item-chunks {
  font-size: 11px;
  color: #3556a3;
  background: #eef4ff;
  border-radius: 8px;
  padding: 1px 6px;
}

.charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
.chart-box  { border: 1px solid #eee; border-radius: 8px; overflow: hidden; }
.chart      { width: 100%; height: 360px; }

/* 关系图全屏 */
.graph-box  { position: relative; }
.fullscreen-btn {
  position: absolute; top: 8px; right: 8px; z-index: 10;
  width: 28px; height: 28px; padding: 4px;
  background: rgba(255,255,255,.85); border: 1px solid #ddd; border-radius: 5px;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
  color: #555; transition: background .15s, color .15s;
}
.fullscreen-btn:hover { background: #5470c6; color: #fff; border-color: #5470c6; }
.fullscreen-btn svg   { width: 16px; height: 16px; }

/* 全屏状态下 chart 撑满 */
.graph-box:fullscreen         { background: #fff; padding: 8px; }
.graph-box:fullscreen .chart  { height: 100vh; }
.graph-box:-webkit-full-screen         { background: #fff; padding: 8px; }
.graph-box:-webkit-full-screen .chart  { height: 100vh; }

.entity-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.entity-table th { background: #f8f8f8; padding: 9px 12px; text-align: left; border-bottom: 2px solid #eee; }
.entity-table td { padding: 8px 12px; border-bottom: 1px solid #f0f0f0; }
.entity-table tr:hover td { background: #fafafa; }
.label-badge { background: #e0e7ff; color: #3730a3; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.raw-label   { color: #888; font-size: 12px; }
.empty       { text-align: center; color: #aaa; padding: 20px !important; }
.empty-hint  { text-align: center; color: #bbb; font-size: 14px; padding: 60px 0; }

/* 实体详情分页控件 */
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 12px;
}
.section-header h3 {
  margin-bottom: 0;
}
.pagination-controls {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 13px;
}
.total-info {
  color: #888;
  font-size: 13px;
}
.page-size-selector {
  display: flex;
  align-items: center;
  gap: 6px;
}
.page-size-label {
  color: #666;
  font-size: 13px;
}
.page-size-btn {
  background: #f5f5f5;
  border: 1px solid #e0e0e0;
  border-radius: 4px;
  padding: 4px 12px;
  cursor: pointer;
  font-size: 12px;
  color: #555;
  transition: all 0.2s;
}
.page-size-btn:hover {
  background: #e8e8e8;
  border-color: #d0d0d0;
}
.page-size-btn.active {
  background: #5470c6;
  border-color: #5470c6;
  color: #fff;
  font-weight: 600;
}
.page-nav {
  display: flex;
  align-items: center;
  gap: 8px;
}
.page-nav button {
  background: #f5f5f5;
  border: 1px solid #e0e0e0;
  border-radius: 4px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 14px;
  color: #555;
  transition: all 0.2s;
}
.page-nav button:hover:not(:disabled) {
  background: #e8e8e8;
  border-color: #d0d0d0;
}
.page-nav button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.page-info {
  font-size: 12px;
  color: #555;
  min-width: 60px;
  text-align: center;
}

@media (max-width: 900px) {
  .eg-root { flex-direction: column; }
  .history-panel { width: 100%; }
  .charts-row { grid-template-columns: 1fr; }
}
</style>

