<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import { isAdmin, authFetch } from '../utils/auth.js'

// 任务统计子页：ner / attack / ti（由 App.vue 二级菜单传入）
const props = defineProps({
  section: { type: String, default: 'ner' },
})
const SECTION_TITLES = {
  ner: 'NER 模型统计',
  attack: 'ATT&CK 模型统计',
  ti: '联网解析统计',
}
const pageTitle = computed(() => SECTION_TITLES[props.section] || SECTION_TITLES.ner)

const stats      = ref({ models: [], total_requests: 0, queue_size: 0 })
const overview   = ref(null)   // /stats/models/overview 增强统计（NER / ATT&CK）
const modelTypes = ref({})     // { modelName -> 'attack' | 'ner' | ... }（overview 不可用时的降级分组）

// NER / ATT&CK 图表
const nerTrendEl = ref(null), nerModelEl = ref(null), nerLabelEl = ref(null), nerSourceEl = ref(null)
const atkTrendEl = ref(null), atkModelEl = ref(null), atkLabelEl = ref(null), atkSourceEl = ref(null)
const chartEls = {
  nerTrend: nerTrendEl, nerModel: nerModelEl, nerLabel: nerLabelEl, nerSource: nerSourceEl,
  atkTrend: atkTrendEl, atkModel: atkModelEl, atkLabel: atkLabelEl, atkSource: atkSourceEl,
}
const charts = {}

// ── 联网解析（TI）统计 ──
const tiStats       = ref(null)
const tiUnavailable = ref(false)
const tiTrendEl = ref(null)
const tiSeedEl  = ref(null)
let tiTrendChart = null
let tiSeedChart  = null

const TI_SESSION_STATES = {
  ok:           { text: '有效',       color: '#27ae60' },
  expired:      { text: '已失效',     color: '#e74c3c' },
  auth_failed:  { text: '认证失败',   color: '#e74c3c' },
  need_manual:  { text: '需人工登录', color: '#f39c12' },
  unknown:      { text: '未知',       color: '#aaa'    },
}
const TI_SEED_NAMES = { ip: 'IP', domain: '域名', file: '样本 Hash' }
const SOURCE_NAMES  = { text: '文本提交', upload: '文件上传', eml: 'EML 邮件', unknown: '历史数据' }

const NER_COLORS = ['#667eea', '#43e97b', '#4facfe', '#764ba2', '#38f9d7', '#00f2fe', '#f7971e']
const ATK_COLORS = ['#f7971e', '#f953c6', '#ffd200', '#b91d73', '#ff6b6b', '#4facfe', '#43e97b']

function tiSessionView(state) {
  return TI_SESSION_STATES[state] || TI_SESSION_STATES.unknown
}

function fmtLatency(ms) {
  if (ms == null) return '—'
  return ms >= 1000 ? (ms / 1000).toFixed(1) + ' s' : Math.round(ms) + ' ms'
}

function fmtTime(s) {
  return s ? s.slice(5, 16).replace('T', ' ') : '—'
}

// overview 分组数据（未加载时给空骨架，模板不用判空）
const EMPTY_GROUP = {
  summary: {
    today_calls: 0, today_success: 0, today_failed: 0, today_cache_hits: 0,
    total_requests: 0, success_rate: 0, avg_latency_ms: null, cache_hit_rate: 0,
  },
  daily_trend: [], model_dist: [], source_dist: [], label_dist: [], models: [],
}
function grp(g) {
  return overview.value?.groups?.[g] || EMPTY_GROUP
}

// 表格行：overview 已加载用增强字段；否则降级为 /stats 基础字段（新列显示 —）
function tableModels(g) {
  if (overview.value) return grp(g).models
  return stats.value.models.filter(
    m => (modelTypes.value[m.model_name] || 'ner') === (g === 'attack' ? 'attack' : 'ner')
  )
}

async function fetchTiStats() {
  try {
    const res = await authFetch('/ti/stats/overview')
    if (res.status === 503) { tiUnavailable.value = true; return }  // TI 未启用
    if (!res.ok) return
    tiStats.value = await res.json()
    await nextTick()  // 板块 v-if 渲染后再挂图表
    if (!tiTrendChart && tiTrendEl.value) tiTrendChart = echarts.init(tiTrendEl.value)
    if (!tiSeedChart && tiSeedEl.value)   tiSeedChart  = echarts.init(tiSeedEl.value)
    tiTrendChart?.setOption(buildTiTrendOption())
    tiSeedChart?.setOption(buildTiSeedOption())
  } catch (e) {
    console.error('TI stats fetch failed:', e)
  }
}

function buildTiTrendOption() {
  const rows = tiStats.value?.daily_trend || []
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['成功调用', '失败调用', '缓存命中'], bottom: 0,
              textStyle: { color: '#555', fontSize: 12 } },
    grid: { left: 44, right: 16, bottom: 52, top: 20 },
    xAxis: { type: 'category', data: rows.map(r => r.day.slice(5)),
             axisLabel: { color: '#555' } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { color: '#555' } },
    series: [
      { name: '成功调用', type: 'bar', stack: 'call',
        data: rows.map(r => r.success), itemStyle: { color: '#43e97b' } },
      { name: '失败调用', type: 'bar', stack: 'call',
        data: rows.map(r => r.fail), itemStyle: { color: '#ff6b6b' } },
      { name: '缓存命中', type: 'bar', stack: 'call',
        data: rows.map(r => r.cache),
        itemStyle: { color: '#4facfe', borderRadius: [4, 4, 0, 0] } },
    ],
  }
}

function buildTiSeedOption() {
  const rows = tiStats.value?.seed_type_dist || []
  return {
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const p = params[0]
        return `${p.name}<br/>任务数: <b>${p.value}</b>`
      }
    },
    grid: { left: 60, right: 24, bottom: 40, top: 20 },
    xAxis: { type: 'category',
             data: rows.map(r => TI_SEED_NAMES[r.seed_type] || r.seed_type),
             axisLabel: { color: '#555' } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { color: '#555' } },
    series: [{
      type: 'bar', barWidth: 56,
      data: rows.map(r => r.count),
      itemStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1,
          [{ offset: 0, color: '#4facfe' }, { offset: 1, color: '#00f2fe' }]),
        borderRadius: [4, 4, 0, 0]
      },
      label: { show: true, position: 'top', color: '#555', fontSize: 12 }
    }],
  }
}

// ── NER / ATT&CK 增强图表 ──

function buildTrendOption(rows) {
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { data: ['成功', '失败', '缓存命中'], bottom: 0,
              textStyle: { color: '#555', fontSize: 12 } },
    grid: { left: 44, right: 16, bottom: 52, top: 20 },
    xAxis: { type: 'category', data: rows.map(r => r.day.slice(5)),
             axisLabel: { color: '#555' } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { color: '#555' } },
    series: [
      { name: '成功', type: 'bar', stack: 'call',
        data: rows.map(r => r.success), itemStyle: { color: '#43e97b' } },
      { name: '失败', type: 'bar', stack: 'call',
        data: rows.map(r => r.fail), itemStyle: { color: '#ff6b6b' } },
      { name: '缓存命中', type: 'bar', stack: 'call',
        data: rows.map(r => r.cache),
        itemStyle: { color: '#4facfe', borderRadius: [4, 4, 0, 0] } },
    ],
  }
}

function buildModelPieOption(rows, colors) {
  return {
    tooltip: { trigger: 'item',
               formatter: p => `${p.name}<br/>${p.value} 次（${p.percent}%）` },
    legend: { bottom: 0, type: 'scroll', textStyle: { color: '#555', fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['32%', '58%'], center: ['50%', '44%'],
      data: rows.map(r => ({ name: r.model_name, value: r.count })),
      label: { color: '#555', fontSize: 11 },
      color: colors,
    }],
  }
}

function buildLabelOption(rows, color) {
  const items = [...rows].reverse()  // 横向条形图，最大值在最上
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 40, bottom: 10, top: 10, containLabel: true },
    xAxis: { type: 'value', minInterval: 1, axisLabel: { color: '#555' } },
    yAxis: { type: 'category', data: items.map(r => r.label),
             axisLabel: { color: '#555', fontSize: 11 } },
    series: [{
      type: 'bar', data: items.map(r => r.count), barMaxWidth: 18,
      itemStyle: { color, borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: 'right', color: '#555', fontSize: 11 },
    }],
  }
}

function buildSourceOption(rows, colors) {
  return {
    tooltip: { trigger: 'item',
               formatter: p => `${p.name}<br/>${p.value} 次（${p.percent}%）` },
    legend: { bottom: 0, textStyle: { color: '#555', fontSize: 12 } },
    series: [{
      type: 'pie', radius: ['28%', '55%'], center: ['50%', '44%'],
      data: rows.map(r => ({ name: SOURCE_NAMES[r.source] || r.source, value: r.count })),
      label: { color: '#555', fontSize: 11 },
      color: colors,
    }],
  }
}

function updateOverviewCharts() {
  const ner = grp('ner'), atk = grp('attack')
  charts.nerTrend?.setOption(buildTrendOption(ner.daily_trend))
  charts.nerModel?.setOption(buildModelPieOption(ner.model_dist, NER_COLORS))
  charts.nerLabel?.setOption(buildLabelOption(ner.label_dist, '#667eea'))
  charts.nerSource?.setOption(buildSourceOption(ner.source_dist, NER_COLORS))
  charts.atkTrend?.setOption(buildTrendOption(atk.daily_trend))
  charts.atkModel?.setOption(buildModelPieOption(atk.model_dist, ATK_COLORS))
  charts.atkLabel?.setOption(buildLabelOption(atk.label_dist, '#f7971e'))
  charts.atkSource?.setOption(buildSourceOption(atk.source_dist, ATK_COLORS))
}

let refreshTimer = null

const isAdminUser = ref(isAdmin())
const users = ref([])
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
    console.error('Fetch users failed:', e)
  }
}

async function fetchModelTypes() {
  try {
    const res = await authFetch('/models')
    if (res.ok) {
      const data = await res.json()
      const map = {}
      for (const m of (data.models_info || [])) {
        map[m.name] = m.type || 'ner'
      }
      modelTypes.value = map
    }
  } catch (e) {
    console.error('Fetch model types failed:', e)
  }
}

async function fetchStats() {
  try {
    let url = '/stats'
    if (selectedUserId.value) {
      url += `?user_id=${selectedUserId.value}`
    }
    const res = await authFetch(url)
    stats.value = await res.json()
  } catch (e) {
    console.error('Stats fetch failed:', e)
  }
}

async function fetchOverview() {
  try {
    let url = '/stats/models/overview?days=30'
    if (selectedUserId.value) {
      url += `&user_id=${selectedUserId.value}`
    }
    const res = await authFetch(url)
    if (!res.ok) return
    overview.value = await res.json()
    updateOverviewCharts()
  } catch (e) {
    console.error('Overview fetch failed:', e)
  }
}

function initCharts() {
  for (const [key, elRef] of Object.entries(chartEls)) {
    if (elRef.value && !charts[key]) charts[key] = echarts.init(elRef.value)
  }
}

onMounted(async () => {
  await nextTick()
  initCharts()
  if (isAdminUser.value) {
    await fetchUsers()
  }
  await fetchModelTypes()
  fetchAll()
  refreshTimer = setInterval(fetchAll, 10000)
})

// 按子页拉取对应数据（TI 为平台级口径，不随用户过滤）
function fetchAll() {
  if (props.section === 'ti') {
    fetchTiStats()
  } else {
    fetchStats()
    fetchOverview()
  }
}

function onUserChange() {
  fetchStats()
  fetchOverview()
}

onUnmounted(() => {
  clearInterval(refreshTimer)
  for (const c of Object.values(charts)) c?.dispose()
  tiTrendChart?.dispose()
  tiSeedChart?.dispose()
})
</script>

<template>
  <div class="model-stats">
    <div class="header-row">
      <h2>{{ pageTitle }}</h2>
      <div v-if="isAdminUser && section !== 'ti'" class="filter-user">
        <span class="filter-label">用户:</span>
        <select v-model="selectedUserId" @change="onUserChange" class="user-select">
          <option value="">全部</option>
          <option v-for="u in users" :key="u.id" :value="u.id">{{ u.username }}</option>
        </select>
      </div>
      <span v-if="section !== 'ti'" class="total">模型总请求: {{ stats.total_requests }}</span>
      <button class="refresh-btn" @click="fetchAll">刷新</button>
    </div>

    <!-- ── NER 模型区域 ── -->
    <template v-if="section === 'ner'">
    <div class="page-note">趋势与标签分布窗口为近 {{ overview?.window_days || 30 }} 天</div>
    <div class="ti-cards">
      <div class="ti-card">
        <div class="ti-card-label">今日调用</div>
        <div class="ti-card-value">{{ grp('ner').summary.today_calls }}</div>
        <div class="ti-card-sub">
          成功 {{ grp('ner').summary.today_success }} / 失败 {{ grp('ner').summary.today_failed }}
        </div>
      </div>
      <div class="ti-card">
        <div class="ti-card-label">今日缓存命中</div>
        <div class="ti-card-value">{{ grp('ner').summary.today_cache_hits }}</div>
        <div class="ti-card-sub">复用历史结果，未实际推理</div>
      </div>
      <div class="ti-card">
        <div class="ti-card-label">成功率（全量）</div>
        <div class="ti-card-value">{{ (grp('ner').summary.success_rate * 100).toFixed(1) }}%</div>
        <div class="ti-card-sub">总请求 {{ grp('ner').summary.total_requests }} 次</div>
      </div>
      <div class="ti-card">
        <div class="ti-card-label">平均延迟（全量）</div>
        <div class="ti-card-value">{{ fmtLatency(grp('ner').summary.avg_latency_ms) }}</div>
        <div class="ti-card-sub">
          缓存命中率 {{ (grp('ner').summary.cache_hit_rate * 100).toFixed(1) }}%
        </div>
      </div>
    </div>
    <div class="charts-row">
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot trend-ti-dot"></span>
          近 {{ overview?.window_days || 30 }} 天调用趋势（成功 / 失败 / 缓存命中）
        </div>
        <div ref="nerTrendEl" class="chart"></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot request-ner-dot"></span>模型调用占比
        </div>
        <div ref="nerModelEl" class="chart"></div>
      </div>
    </div>
    <div class="charts-row">
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot latency-ner-dot"></span>实体标签分布（Top 15）
        </div>
        <div ref="nerLabelEl" class="chart"></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot seed-ti-dot"></span>来源渠道分布
        </div>
        <div ref="nerSourceEl" class="chart"></div>
      </div>
    </div>
    <table class="stats-table">
      <thead>
        <tr>
          <th>模型</th><th>总请求</th><th>成功</th><th>失败</th><th>成功率</th>
          <th>缓存命中率</th><th>平均延迟</th><th>P95</th><th>平均实体数</th>
          <th>今日</th><th>最近调用</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in tableModels('ner')" :key="m.model_name">
          <td>
            <strong>{{ m.model_name }}</strong>
            <div v-if="m.routed && m.routed.length" class="cell-sub">
              → {{ m.routed.map(r => `${r.routed_model} ${r.count}`).join(' · ') }}
            </div>
          </td>
          <td>{{ m.total_requests }}</td>
          <td class="success">{{ m.successful_requests }}</td>
          <td class="fail">{{ m.failed_requests }}</td>
          <td>{{ (m.success_rate * 100).toFixed(1) }}%</td>
          <td>{{ m.cache_hit_rate != null ? (m.cache_hit_rate * 100).toFixed(1) + '%' : '—' }}</td>
          <td>{{ fmtLatency(m.avg_latency_ms) }}</td>
          <td>{{ fmtLatency(m.p95_latency_ms) }}</td>
          <td>{{ m.avg_entities != null ? m.avg_entities : '—' }}</td>
          <td>{{ m.today_calls != null ? m.today_calls : '—' }}</td>
          <td>{{ fmtTime(m.last_called_at) }}</td>
        </tr>
        <tr v-if="!tableModels('ner').length">
          <td colspan="11" class="empty">暂无 NER 模型数据</td>
        </tr>
      </tbody>
    </table>
    </template>

    <!-- ── ATT&CK 模型区域 ── -->
    <template v-if="section === 'attack'">
    <div class="page-note">趋势与标签分布窗口为近 {{ overview?.window_days || 30 }} 天</div>
    <div class="ti-cards">
      <div class="ti-card">
        <div class="ti-card-label">今日调用</div>
        <div class="ti-card-value">{{ grp('attack').summary.today_calls }}</div>
        <div class="ti-card-sub">
          成功 {{ grp('attack').summary.today_success }} / 失败 {{ grp('attack').summary.today_failed }}
        </div>
      </div>
      <div class="ti-card">
        <div class="ti-card-label">今日缓存命中</div>
        <div class="ti-card-value">{{ grp('attack').summary.today_cache_hits }}</div>
        <div class="ti-card-sub">复用历史结果，未实际推理</div>
      </div>
      <div class="ti-card">
        <div class="ti-card-label">成功率（全量）</div>
        <div class="ti-card-value">{{ (grp('attack').summary.success_rate * 100).toFixed(1) }}%</div>
        <div class="ti-card-sub">总请求 {{ grp('attack').summary.total_requests }} 次</div>
      </div>
      <div class="ti-card">
        <div class="ti-card-label">平均延迟（全量）</div>
        <div class="ti-card-value">{{ fmtLatency(grp('attack').summary.avg_latency_ms) }}</div>
        <div class="ti-card-sub">
          缓存命中率 {{ (grp('attack').summary.cache_hit_rate * 100).toFixed(1) }}%
        </div>
      </div>
    </div>
    <div class="charts-row">
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot trend-ti-dot"></span>
          近 {{ overview?.window_days || 30 }} 天调用趋势（成功 / 失败 / 缓存命中）
        </div>
        <div ref="atkTrendEl" class="chart"></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot request-atk-dot"></span>模型调用占比
        </div>
        <div ref="atkModelEl" class="chart"></div>
      </div>
    </div>
    <div class="charts-row">
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot latency-atk-dot"></span>实体标签分布（Top 15）
        </div>
        <div ref="atkLabelEl" class="chart"></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">
          <span class="chart-dot seed-ti-dot"></span>来源渠道分布
        </div>
        <div ref="atkSourceEl" class="chart"></div>
      </div>
    </div>
    <table class="stats-table">
      <thead>
        <tr>
          <th>模型</th><th>总请求</th><th>成功</th><th>失败</th><th>成功率</th>
          <th>缓存命中率</th><th>平均延迟</th><th>P95</th><th>平均实体数</th>
          <th>今日</th><th>最近调用</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in tableModels('attack')" :key="m.model_name">
          <td>
            <strong>{{ m.model_name }}</strong>
            <div v-if="m.routed && m.routed.length" class="cell-sub">
              → {{ m.routed.map(r => `${r.routed_model} ${r.count}`).join(' · ') }}
            </div>
          </td>
          <td>{{ m.total_requests }}</td>
          <td class="success">{{ m.successful_requests }}</td>
          <td class="fail">{{ m.failed_requests }}</td>
          <td>{{ (m.success_rate * 100).toFixed(1) }}%</td>
          <td>{{ m.cache_hit_rate != null ? (m.cache_hit_rate * 100).toFixed(1) + '%' : '—' }}</td>
          <td>{{ fmtLatency(m.avg_latency_ms) }}</td>
          <td>{{ fmtLatency(m.p95_latency_ms) }}</td>
          <td>{{ m.avg_entities != null ? m.avg_entities : '—' }}</td>
          <td>{{ m.today_calls != null ? m.today_calls : '—' }}</td>
          <td>{{ fmtTime(m.last_called_at) }}</td>
        </tr>
        <tr v-if="!tableModels('attack').length">
          <td colspan="11" class="empty">暂无 ATT&amp;CK 模型数据</td>
        </tr>
      </tbody>
    </table>
    </template>

    <!-- ── 联网解析统计（TI 平台级运营指标，不随用户过滤） ── -->
    <template v-if="section === 'ti' && tiStats && !tiUnavailable">
      <div class="page-note">平台级指标，不随用户过滤；外部调用 / 缓存窗口为近 {{ tiStats.window_days }} 天</div>

      <!-- 概览卡片 -->
      <div class="ti-cards">
        <div class="ti-card">
          <div class="ti-card-label">今日外部调用</div>
          <div class="ti-card-value">{{ tiStats.today.external_calls }}</div>
          <div class="ti-card-sub">成功 {{ tiStats.today.success }} / 失败 {{ tiStats.today.fail }}</div>
        </div>
        <div class="ti-card">
          <div class="ti-card-label">今日缓存命中</div>
          <div class="ti-card-value">{{ tiStats.today.cache_hits }}</div>
          <div class="ti-card-sub">复用历史结果，未触达平台</div>
        </div>
        <div class="ti-card">
          <div class="ti-card-label">外部调用成功率（全量）</div>
          <div class="ti-card-value">{{ (tiStats.totals.success_rate * 100).toFixed(1) }}%</div>
          <div class="ti-card-sub">
            {{ tiStats.totals.external_success }} / {{ tiStats.totals.external_calls }} 次
          </div>
        </div>
        <div class="ti-card">
          <div class="ti-card-label">平均采集耗时</div>
          <div class="ti-card-value">{{ fmtLatency(tiStats.totals.avg_latency_ms) }}</div>
          <div class="ti-card-sub">完成任务均值，共 {{ tiStats.totals.tasks_done }} 个</div>
        </div>
      </div>

      <!-- 趋势 + 种子分布 -->
      <div class="charts-row">
        <div class="chart-card">
          <div class="chart-title">
            <span class="chart-dot trend-ti-dot"></span>
            近 {{ tiStats.window_days }} 天调用趋势（成功 / 失败 / 缓存命中）
          </div>
          <div ref="tiTrendEl" class="chart"></div>
        </div>
        <div class="chart-card">
          <div class="chart-title">
            <span class="chart-dot seed-ti-dot"></span>任务种子类型分布
          </div>
          <div ref="tiSeedEl" class="chart"></div>
        </div>
      </div>

      <!-- 平台明细 -->
      <table class="stats-table">
        <thead>
          <tr>
            <th>平台</th><th>会话</th><th>今日调用 / 上限</th><th>今日缓存</th>
            <th>外部调用</th><th>成功率</th><th>缓存命中率</th><th>平均耗时</th>
            <th>任务（完成/失败）</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in tiStats.platforms" :key="p.platform">
            <td><strong>{{ p.name }}</strong></td>
            <td>
              <span class="session-dot"
                    :style="{ background: tiSessionView(p.login_state).color }"></span>
              {{ tiSessionView(p.login_state).text }}
            </td>
            <td>{{ p.today_calls }} / {{ p.daily_limit }}</td>
            <td>{{ p.today_cache_hits }}</td>
            <td>{{ p.calls }}<span class="cell-sub">（{{ p.success }} 成 / {{ p.fail }} 败）</span></td>
            <td>{{ (p.success_rate * 100).toFixed(1) }}%</td>
            <td>{{ (p.hit_rate * 100).toFixed(1) }}%<span class="cell-sub">（{{ p.cache_hits }} 次）</span></td>
            <td>{{ fmtLatency(p.avg_latency_ms) }}</td>
            <td>{{ p.tasks_done }} / {{ p.tasks_failed }}</td>
          </tr>
        </tbody>
      </table>
    </template>
    <div v-if="section === 'ti' && (tiUnavailable || !tiStats)" class="empty ti-empty">
      {{ tiUnavailable ? '联网解析（TI）模块未启用' : '统计数据加载中…' }}
    </div>
  </div>
</template>

<style scoped>
.model-stats {
  background: #fff;
  border-radius: 8px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
}
.header-row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 20px;
}
h2 { font-size: 18px; flex: 1; }

.filter-user { display: flex; align-items: center; gap: 8px; margin-right: 16px; }
.filter-label { font-size: 14px; color: #666; }
.user-select {
  padding: 6px 12px;
  border: 1px solid #ddd;
  border-radius: 4px;
  background: #fff;
  font-size: 14px;
  cursor: pointer;
  color: #333;
  outline: none;
}
.user-select:focus { border-color: #667eea; }
.total  { color: #888; font-size: 14px; }
.refresh-btn {
  padding: 6px 14px;
  border: 1px solid #ddd;
  border-radius: 4px;
  cursor: pointer;
  background: #fff;
}
.refresh-btn:hover { background: #f5f5f5; }

/* 子页说明行 */
.page-note {
  font-size: 12px;
  color: #999;
  margin: -8px 0 14px;
}
.ti-empty { padding: 48px 0 !important; }

/* 两图并排 */
.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.chart-card {
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  padding: 12px 12px 4px;
  background: #fafafa;
}
.chart-title {
  font-size: 13px;
  font-weight: 600;
  color: #444;
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.chart-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.latency-ner-dot { background: linear-gradient(135deg, #667eea, #764ba2); }
.request-ner-dot { background: linear-gradient(135deg, #43e97b, #38f9d7); }
.latency-atk-dot { background: linear-gradient(135deg, #f7971e, #ffd200); }
.request-atk-dot { background: linear-gradient(135deg, #f953c6, #b91d73); }
.chart { width: 100%; height: 260px; }

/* 表格 */
.stats-table { width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 8px; }
.stats-table th {
  background: #f8f8f8;
  padding: 10px 12px;
  text-align: left;
  border-bottom: 2px solid #eee;
  white-space: nowrap;
}
.stats-table td { padding: 10px 12px; border-bottom: 1px solid #eee; }
.stats-table tr:hover td { background: #fafafa; }
.success { color: #27ae60; font-weight: 600; }
.fail    { color: #e74c3c; }
.empty   { text-align: center; color: #aaa; padding: 24px !important; }

/* ── 概览卡 / 联网解析统计板块 ── */
.ti-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.ti-card {
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  padding: 14px 16px;
  background: #fafafa;
}
.ti-card-label { font-size: 13px; color: #777; margin-bottom: 6px; }
.ti-card-value { font-size: 24px; font-weight: 700; color: #333; }
.ti-card-sub   { font-size: 12px; color: #999; margin-top: 4px; }
.trend-ti-dot { background: linear-gradient(135deg, #43e97b, #4facfe); }
.seed-ti-dot  { background: linear-gradient(135deg, #4facfe, #00f2fe); }
.session-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
}
.cell-sub { font-size: 12px; color: #999; }
</style>
