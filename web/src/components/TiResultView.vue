<script setup>
/**
 * 联网情报任务详情 —— 按平台原生页面风格展示。
 *
 * 还原依据：ti_data/recon/<platform>/display-spec.md（真实页面截图调研）
 *  - 微步在线：白底判定卡片（圆形判定徽章 + 概览计数条 + tab 区块）
 *  - 奇安信：深蓝研判区（判定印章 + kv 深蓝面板）+ 白色折叠卡片 + AI 摘要卡
 * 数据：/ti/tasks/{id} 的 sections（kv / table / text 三种区块），按区块名映射到平台布局。
 */
import { ref, computed } from 'vue'
import VueJsonPretty from 'vue-json-pretty'
import 'vue-json-pretty/lib/styles.css'
import TiSectionBody from './TiSectionBody.vue'

const props = defineProps({
  task: { type: Object, required: true },
})

const PLATFORM_META = {
  threatbook: { name: '微步在线', color: '#d4380d', bg: '#fff2e8' },
  qianxin:    { name: '奇安信',   color: '#008cff', bg: '#e8f4ff' },
}
const SEED_TYPE_LABELS = { ip: 'IP 地址', domain: '域名', file: '样本哈希' }

const isQianxin = computed(() => props.task.platform === 'qianxin')
const meta = computed(() => PLATFORM_META[props.task.platform] || { name: props.task.platform, color: '#555', bg: '#eee' })
const sections = computed(() => props.task.sections || [])

function findSection(name) {
  return sections.value.find(s => s.name === name)
}

// ─── 判定结论提取 ───────────────────────────────────────────────────────────
const verdict = computed(() => {
  // 微步：「威胁判定」区块首行
  const s = findSection('威胁判定')
  if (s?.format === 'text' && s.content) {
    const line = s.content.split('\n').map(l => l.trim()).filter(Boolean)[0]
    if (line) return line
  }
  // 奇安信：威胁概览 kv 中的判定类键
  const ov = findSection('威胁概览')
  if (ov?.format === 'kv' && ov.items) {
    for (const k of Object.keys(ov.items)) {
      if (/判定|威胁等级|结论/.test(k)) return String(ov.items[k])
    }
  }
  // 兜底：AI 研判摘要首句「判定 xxx 为【恶意】」（file 种子前缀「文件」+64位哈希，放宽到 80 字符）
  const ai = findSection('AI研判摘要')
  if (ai?.content) {
    const m = ai.content.match(/判定[^。]{0,80}?为[【\s]*(恶意|可疑|安全|危险|正常)/)
    if (m) return m[1]
  }
  return ''
})

const verdictCls = computed(() => {
  const v = verdict.value
  if (/恶意|高危|黑/.test(v)) return 'malicious'
  if (/可疑|中危|风险|低危/.test(v)) return 'suspicious'
  if (/安全|正常|白|良/.test(v)) return 'safe'
  return 'unknown'
})

// ─── 奇安信：概览 kv（判定类键剔除，避免与徽章重复）─────────────────────────
const qxOverviewItems = computed(() => {
  const ov = findSection('威胁概览')
  if (!ov || ov.format !== 'kv' || !ov.items) return {}
  const out = {}
  for (const [k, v] of Object.entries(ov.items)) {
    if (/判定|威胁等级|结论/.test(k)) continue
    out[k] = v
  }
  return out
})

// ─── 微步 ip/domain：tab 区块分组 ───────────────────────────────────────────
const TB_TAB_NAMES = ['网页结果', '攻击画像', '域名解析', 'WHOIS', '解析域名', '子域名',
                      '数字证书', '相关样本', '资产测绘', '端口详情']
const tbTabs = computed(() => {
  if (isQianxin.value || props.task.seed_type === 'file') return []
  return sections.value.filter(s => TB_TAB_NAMES.includes(s.name))
})
const tbTopSections = computed(() => {
  // 微步 ip/domain 头部区：判定/基础信息/概览/情报洞察/情报速递/用户标签
  if (isQianxin.value || props.task.seed_type === 'file') return []
  return sections.value.filter(s => !TB_TAB_NAMES.includes(s.name) && s.name !== '威胁判定')
})
const activeTbTab = ref('')
function tbTabContent(name) { activeTbTab.value = name }
const currentTbTab = computed(() => {
  if (!tbTabs.value.length) return null
  if (activeTbTab.value && tbTabs.value.some(s => s.name === activeTbTab.value)) {
    return findSection(activeTbTab.value)
  }
  return tbTabs.value[0]
})

// ─── 奇安信 file：tab 分组（威胁情报为默认聚合 tab；r4 起子模块细分，
//     按「tab → 区块名列表」映射；同时兼容 r3 旧名（缓存任务））─────────────
const QX_FILE_TAB_MAP = {
  '威胁情报': ['威胁研判标签', 'AI研判摘要', '情报研判标签', '情报参考标签'],
  '检测结果': ['检测概览', '引擎检测明细', '检测结果'],
  '基本信息': ['文件信息', 'Exiftool文件元数据', '基本信息'],
  '主机行为': ['行为异常分析', '行为明细', '资源信息', '进程树', '主机行为'],
  '网络行为': ['DNS', '下载链接', '网络行为'],
  'QAX情报': ['QAX情报'],
  '网页结果': ['网页结果'],
}
const QX_FILE_TABS = Object.keys(QX_FILE_TAB_MAP)
const activeQxTab = ref('威胁情报')
const isQxFile = computed(() => isQianxin.value && props.task.seed_type === 'file')
const qxFileTabSections = computed(() => {
  if (!isQxFile.value) return []
  const names = QX_FILE_TAB_MAP[activeQxTab.value] || []
  return sections.value.filter(s => names.includes(s.name))
})
const qxMainSections = computed(() => {
  // 奇安信 ip/domain 内容区：除威胁概览外的所有区块
  if (!isQianxin.value || isQxFile.value) return []
  return sections.value.filter(s => s.name !== '威胁概览')
})

// ─── 奇安信 ip/domain：tab 分组（与原页面侧导航一致；r6 起按 tab 展示）──────
// 威胁情报为默认聚合 tab；其余 tab 与区块同名（历史注册信息归入注册信息）
const QX_INTEL_SECTION_NAMES = ['威胁研判确定性情报标签', 'AI研判摘要', '情报研判标签', '情报参考标签',
                                '相关安全报告', '攻击者画像', 'IOC详情', '相关样本']
const QX_PAGE_TAB_ORDERS = {
  ip:     ['威胁情报', 'QAX情报', '域名反查', 'C段情报', 'WHOIS', '主机信息', '数字证书', '近期活动', '网页结果'],
  domain: ['威胁情报', 'QAX情报', '域名解析', '注册信息', '关联域名', '数字证书', '近期活动', '网页结果'],
}
function qxSectionTab(name) {
  if (QX_INTEL_SECTION_NAMES.includes(name)) return '威胁情报'
  if (name === '历史注册信息') return '注册信息'
  return name
}
const qxPageTabs = computed(() => {
  // 只展示有数据区块的 tab（空 tab 自动隐藏）
  const order = QX_PAGE_TAB_ORDERS[props.task.seed_type] || []
  const have = new Set(qxMainSections.value.map(s => qxSectionTab(s.name)))
  return order.filter(t => have.has(t))
})
const activeQxPageTab = ref('威胁情报')
const qxPageTabSections = computed(() => {
  if (!qxPageTabs.value.length) return qxMainSections.value  // 无映射时平铺兜底
  const tab = qxPageTabs.value.includes(activeQxPageTab.value) ? activeQxPageTab.value : qxPageTabs.value[0]
  return qxMainSections.value.filter(s => qxSectionTab(s.name) === tab)
})

// ─── 微步 file：纵向报告卡片（全部区块顺序平铺）─────────────────────────────
const tbFileSections = computed(() => {
  if (isQianxin.value || props.task.seed_type !== 'file') return []
  return sections.value
})

// ─── 折叠状态 ───────────────────────────────────────────────────────────────
const closedSet = ref(new Set())
function toggle(key) {
  const s = new Set(closedSet.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  closedSet.value = s
}
function isOpen(key) { return !closedSet.value.has(key) }

function kvText(v) {
  if (Array.isArray(v)) return v.join('、')
  if (v === null || v === undefined || v === '') return '—'
  return String(v)
}
function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts).toLocaleString('zh-CN')
}

// ─── 原始 JSON（task.raw：平台接口原始报文；默认折叠，高亮展示）───────────────
const rawJson = computed(() => props.task.raw ?? null)
const rawOpen = ref(false)
const copyTip = ref('')
async function copyRaw() {
  if (!rawJson.value) return
  try {
    await navigator.clipboard.writeText(JSON.stringify(rawJson.value, null, 2))
    copyTip.value = '已复制'
  } catch {
    copyTip.value = '复制失败'
  }
  setTimeout(() => (copyTip.value = ''), 1500)
}

// 下载文件名：平台-类型-[种子值].json，如 奇安信-IP-[1.1.1.1].json
const RAW_TYPE_LABELS = { ip: 'IP', domain: '域名', file: '样本哈希' }
const rawDownloadName = computed(() => {
  const plat = (PLATFORM_META[props.task.platform] || {}).name || props.task.platform
  const type = RAW_TYPE_LABELS[props.task.seed_type] || props.task.seed_type
  return `${plat}-${type}-[${props.task.seed_value}].json`
})
function downloadRaw() {
  if (!rawJson.value) return
  const blob = new Blob([JSON.stringify(rawJson.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = rawDownloadName.value
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="ti-view" :class="task.platform">
    <!-- ═══════════ 奇安信：深蓝研判区 ═══════════ -->
    <div v-if="isQianxin" class="qx-header">
      <div class="qx-header-main">
        <div class="qx-stamp" :class="verdictCls">
          <span class="qx-stamp-word">{{ verdict || '未知' }}</span>
        </div>
        <div class="qx-title-col">
          <div class="qx-seed">{{ task.seed_value }}</div>
          <div class="qx-seed-meta">
            <span>{{ SEED_TYPE_LABELS[task.seed_type] || task.seed_type }}</span>
            <span>来源：奇安信威胁情报中心</span>
          </div>
          <!-- kv 深蓝面板 -->
          <div v-if="Object.keys(qxOverviewItems).length" class="qx-kv-panel" :class="{ 'two-col': task.seed_type === 'file' }">
            <div v-for="(v, k) in qxOverviewItems" :key="k" class="qx-kv-item">
              <span class="qx-kv-key">{{ k }}</span>
              <span class="qx-kv-value">{{ kvText(v) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══════════ 微步：白底判定卡片 ═══════════ -->
    <div v-else class="tb-header">
      <div class="tb-verdict-row">
        <div class="tb-stamp" :class="verdictCls">{{ verdict || '—' }}</div>
        <div class="tb-title-col">
          <div class="tb-seed">{{ task.seed_value }}</div>
          <div class="tb-seed-meta">
            <span class="tb-plat-tag">微步在线</span>
            <span>{{ SEED_TYPE_LABELS[task.seed_type] || task.seed_type }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ── 通用状态行 ── -->
    <div class="status-row">
      <span :class="['status-chip', task.status]">{{ { pending: '排队中', running: '查询中', done: '完成', failed: '失败' }[task.status] || task.status }}</span>
      <span v-if="task.latency_ms != null" class="meta-item">采集 {{ Math.round(task.latency_ms) }} ms</span>
      <span class="meta-item">{{ fmtTime(task.created_at) }}</span>
      <span v-if="task.diagnostics?.selector_version" class="meta-item">selector {{ task.diagnostics.selector_version }}</span>
    </div>

    <div v-if="task.status === 'failed'" class="fail-card">{{ task.error_msg || '采集失败' }}</div>
    <div v-if="task.status === 'pending' || task.status === 'running'" class="pending-card">
      <span class="spinner"></span> 浏览器采集中（同平台串行 + 随机间隔），页面自动刷新…
    </div>

    <template v-if="task.status === 'done'">
      <!-- ═══════════ 奇安信 ip/domain：tab 主体（与原页面侧导航一致）═══════════ -->
      <template v-if="isQianxin && !isQxFile">
        <div v-if="qxPageTabs.length" class="qx-tabbar">
          <button v-for="t in qxPageTabs" :key="t"
                  :class="{ active: (qxPageTabs.includes(activeQxPageTab) ? activeQxPageTab : qxPageTabs[0]) === t }"
                  @click="activeQxPageTab = t">{{ t }}</button>
        </div>
        <div v-for="(s, i) in qxPageTabSections" :key="i" class="qx-card">
          <div class="qx-card-head" @click="toggle('m' + i)">
            <span class="qx-card-title">
              <span v-if="s.name === 'AI研判摘要'" class="ai-icon">🤖</span>{{ s.name }}
            </span>
            <span class="qx-card-suffix" v-if="s.format === 'table' && s.rows">{{ s.rows.length }} 条</span>
            <span class="card-arrow">{{ isOpen('m' + i) ? '▾' : '▸' }}</span>
          </div>
          <div v-if="isOpen('m' + i)" class="qx-card-body">
            <div v-if="s.name === 'AI研判摘要'" class="ai-panel">
              <div class="ai-disclaimer">以下内容由人工智能模型自动整合生成，仅供参考</div>
              <pre class="sec-text">{{ s.content }}</pre>
            </div>
            <TiSectionBody v-else :section="s" />
          </div>
        </div>
      </template>

      <!-- ═══════════ 奇安信 file：tab 主体 ═══════════ -->
      <template v-else-if="isQxFile">
        <div class="qx-tabbar">
          <button v-for="t in QX_FILE_TABS" :key="t"
                  :class="{ active: activeQxTab === t }"
                  @click="activeQxTab = t">{{ t }}</button>
        </div>
        <div v-for="(s, i) in qxFileTabSections" :key="i" class="qx-card">
          <div class="qx-card-head" @click="toggle('f' + activeQxTab + i)">
            <span class="qx-card-title">
              <span v-if="s.name === 'AI研判摘要'" class="ai-icon">🤖</span>{{ s.name }}
            </span>
            <span class="qx-card-suffix" v-if="s.format === 'table' && s.rows">{{ s.rows.length }} 条</span>
            <span class="card-arrow">{{ isOpen('f' + activeQxTab + i) ? '▾' : '▸' }}</span>
          </div>
          <div v-if="isOpen('f' + activeQxTab + i)" class="qx-card-body">
            <div v-if="s.name === 'AI研判摘要'" class="ai-panel">
              <div class="ai-disclaimer">以下内容由人工智能模型自动整合生成，仅供参考</div>
              <pre class="sec-text">{{ s.content }}</pre>
            </div>
            <TiSectionBody v-else :section="s" />
          </div>
        </div>
        <div v-if="!qxFileTabSections.length" class="empty">该标签页暂无内容</div>
      </template>

      <!-- ═══════════ 微步 ip/domain：头部区块 + tab 区块 ═══════════ -->
      <template v-else-if="!isQianxin && task.seed_type !== 'file'">
        <div v-for="(s, i) in tbTopSections" :key="i" class="tb-card">
          <div class="tb-card-head" @click="toggle('t' + i)">
            <span class="tb-card-title">{{ s.name }}</span>
            <span class="card-arrow">{{ isOpen('t' + i) ? '▾' : '▸' }}</span>
          </div>
          <div v-if="isOpen('t' + i)" class="tb-card-body">
            <!-- 威胁概览：计数条（大数字 + 标签） -->
            <div v-if="s.name === '威胁概览' && s.format === 'kv'" class="tb-count-strip">
              <div v-for="(v, k) in (s.items || {})" :key="k" class="tb-count-item">
                <div class="tb-count-value">{{ kvText(v) }}</div>
                <div class="tb-count-key">{{ k }}</div>
              </div>
            </div>
            <TiSectionBody :section="s" />
          </div>
        </div>

        <!-- tab 导航 + 面板 -->
        <div v-if="tbTabs.length" class="tb-tabs-card">
          <div class="tb-tabbar">
            <button v-for="s in tbTabs" :key="s.name"
                    :class="{ active: (currentTbTab && currentTbTab.name === s.name) }"
                    @click="tbTabContent(s.name)">
              {{ s.name }}<em v-if="s.format === 'table' && s.rows" class="tb-tab-count">{{ s.rows.length }}</em>
            </button>
          </div>
          <div v-if="currentTbTab" class="tb-tab-panel">
            <TiSectionBody :section="currentTbTab" />
          </div>
        </div>
      </template>

      <!-- ═══════════ 微步 file：沙箱报告纵向卡片 ═══════════ -->
      <template v-else>
        <div v-for="(s, i) in tbFileSections" :key="i" class="tb-card">
          <div class="tb-card-head" @click="toggle('s' + i)">
            <span class="tb-card-title">{{ s.name }}</span>
            <span class="qx-card-suffix" v-if="s.format === 'table' && s.rows">{{ s.rows.length }} 条</span>
            <span class="card-arrow">{{ isOpen('s' + i) ? '▾' : '▸' }}</span>
          </div>
          <div v-if="isOpen('s' + i)" class="tb-card-body">
            <TiSectionBody :section="s" />
          </div>
        </div>
      </template>

      <div v-if="!sections.length" class="empty">平台未返回内容</div>

      <!-- ═══════════ 原始 JSON（平台接口原始报文，底部高亮折叠卡）═══════════ -->
      <div class="raw-json-card">
        <div class="raw-json-head" @click="rawOpen = !rawOpen">
          <span class="raw-json-title">原始 JSON（raw）</span>
          <span class="raw-json-sub">{{ rawJson ? '平台接口原始报文' : '本次采集无 raw' }}</span>
          <button v-if="rawJson" class="raw-copy-btn" @click.stop="copyRaw">{{ copyTip || '复制' }}</button>
          <button v-if="rawJson" class="raw-copy-btn" :title="rawDownloadName" @click.stop="downloadRaw">下载</button>
          <span class="card-arrow">{{ rawOpen ? '▾' : '▸' }}</span>
        </div>
        <div v-if="rawOpen" class="raw-json-body">
          <VueJsonPretty v-if="rawJson" :data="rawJson" :deep="1" :show-length="true" class="json-viewer" />
          <div v-else class="raw-empty">
            平台页面未拦截到数据接口时，本次采集无原始 JSON；上方 sections 已包含全部解析结果。
          </div>
        </div>
      </div>
    </template>
  </div>
</template>



<style scoped>
.ti-view { display: flex; flex-direction: column; gap: 12px; }

/* ═══════ 奇安信：深蓝研判区 ═══════ */
.qx-header {
  background: linear-gradient(135deg, #0b1a39 0%, #083060 100%);
  border-radius: 8px; padding: 24px; color: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,.15);
}
.qx-header-main { display: flex; gap: 24px; align-items: flex-start; }
.qx-stamp {
  width: 96px; height: 96px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  border: 4px solid rgba(255,255,255,.35);
  font-size: 20px; font-weight: 700;
}
.qx-stamp.malicious { background: #ff4c4c; border-color: rgba(255,76,76,.4); }
.qx-stamp.suspicious { background: #ff8200; border-color: rgba(255,130,0,.4); }
.qx-stamp.safe { background: #39ba7c; border-color: rgba(57,186,124,.4); }
.qx-stamp.unknown { background: #8c8c8c; border-color: rgba(140,140,140,.4); }
.qx-stamp-word { text-shadow: 0 1px 2px rgba(0,0,0,.3); }
.qx-title-col { flex: 1; min-width: 0; }
.qx-seed { font-size: 22px; font-weight: 700; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.qx-seed-meta { display: flex; gap: 16px; margin-top: 6px; font-size: 12px; color: rgba(255,255,255,.65); }
.qx-kv-panel {
  margin-top: 16px; background: #23314d; border-radius: 8px; padding: 14px 18px;
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px 24px;
}
.qx-kv-panel.two-col { grid-template-columns: repeat(2, 1fr); }
.qx-kv-item { display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.qx-kv-key { font-size: 12px; font-weight: 600; color: rgba(255,255,255,.85); white-space: nowrap; flex-shrink: 0; }
.qx-kv-value { font-size: 12px; color: rgba(255,255,255,.6); word-break: break-all; }

/* 奇安信白色折叠卡片 */
.qx-card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; }
.qx-card-head { display: flex; align-items: center; gap: 10px; padding: 13px 16px; cursor: pointer; user-select: none; }
.qx-card-head:hover { background: #fafbfc; }
.qx-card-title { font-size: 15px; font-weight: 600; color: #333; flex: 1; }
.ai-icon { margin-right: 6px; }
.qx-card-suffix { font-size: 12px; color: #999; }
.qx-card-body { border-top: 1px solid #f0f0f0; }
.ai-panel { background: #f4f6f8; }
.ai-disclaimer { padding: 10px 16px 0; font-size: 12px; color: #999; text-align: right; }

/* 奇安信 tab 条（file 页） */
.qx-tabbar {
  display: flex; gap: 4px; background: linear-gradient(135deg, #0b1a39, #083060);
  border-radius: 8px; padding: 8px;
}
.qx-tabbar button {
  background: transparent; border: none; color: rgba(255,255,255,.75);
  font-size: 13px; padding: 7px 18px; border-radius: 16px; cursor: pointer; transition: all .15s;
}
.qx-tabbar button:hover { color: #fff; }
.qx-tabbar button.active { background: #fff; color: #008cff; font-weight: 600; }

/* 标签 chip */
.chip-cloud { display: flex; flex-wrap: wrap; gap: 8px; padding: 14px 16px; }
.tag-chip { font-size: 12px; padding: 3px 12px; border-radius: 12px; border: 1px solid; }
.chip-red { color: #ec4e3b; border-color: #ec4e3b; background: rgba(236,78,59,.06); }
.chip-blue { color: #008cff; border-color: #008cff; background: rgba(0,140,255,.06); }
.chip-tb { color: #d4380d; border-color: #f0c0a8; background: #fff7f0; }
.no-data { color: #999; font-size: 13px; }

/* ═══════ 微步：白底判定卡片 ═══════ */
.tb-header {
  background: #fff; border-radius: 8px; padding: 20px 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1); border-top: 3px solid #d4380d;
}
.tb-verdict-row { display: flex; align-items: center; gap: 18px; }
/* 微步判定圆盘：白心 + 亮色环 + 判定色字（还原 judgments-N 同心圆盘的简化版） */
.tb-stamp {
  width: 76px; height: 76px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; font-weight: 700; background: #fff;
  border: 5px solid #babbc4;
}
.tb-stamp.malicious { color: #d81414; border-color: #f01414; box-shadow: 0 0 0 4px rgba(240,20,20,.12); }
.tb-stamp.suspicious { color: #d46b08; border-color: #eb9a4d; box-shadow: 0 0 0 4px rgba(235,154,77,.12); }
.tb-stamp.safe { color: #10b070; border-color: #10b070; box-shadow: 0 0 0 4px rgba(16,176,112,.12); }
.tb-stamp.unknown { color: #999; border-color: #babbc4; box-shadow: 0 0 0 4px rgba(186,187,196,.15); }
.tb-title-col { min-width: 0; }
.tb-seed { font-size: 20px; font-weight: 700; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #222; word-break: break-all; }
.tb-seed-meta { display: flex; gap: 10px; margin-top: 6px; align-items: center; font-size: 12px; color: #999; }
.tb-plat-tag { font-size: 12px; padding: 2px 10px; border-radius: 10px; font-weight: 600; background: #fff2e8; color: #d4380d; }

/* 微步卡片 */
.tb-card, .tb-tabs-card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; }
.tb-card-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; cursor: pointer; user-select: none; }
.tb-card-head:hover { background: #fafafa; }
.tb-card-title { font-size: 14px; font-weight: 600; color: #333; border-left: 3px solid #d4380d; padding-left: 10px; flex: 1; }
.tb-card-body { border-top: 1px solid #f0f0f0; }

/* 微步概览计数条 */
.tb-count-strip { display: flex; flex-wrap: wrap; }
.tb-count-item { flex: 1 1 110px; text-align: center; padding: 14px 8px; border-right: 1px solid #f5f5f5; }
.tb-count-item:last-child { border-right: none; }
.tb-count-value { font-size: 20px; font-weight: 700; color: #333; }
.tb-count-key { font-size: 12px; color: #999; margin-top: 4px; }

/* 微步 tab 条 */
.tb-tabbar { display: flex; flex-wrap: wrap; border-bottom: 1px solid #f0f0f0; padding: 0 8px; }
.tb-tabbar button {
  background: transparent; border: none; border-bottom: 2px solid transparent;
  color: #666; font-size: 13px; padding: 12px 16px; cursor: pointer; transition: all .15s;
}
.tb-tabbar button:hover { color: #d4380d; }
.tb-tabbar button.active { color: #d4380d; border-bottom-color: #d4380d; font-weight: 600; }
.tb-tab-count { font-style: normal; font-size: 11px; color: #bbb; margin-left: 4px; }
.tb-tabbar button.active .tb-tab-count { color: #d4380d; }
.tb-tab-panel { min-height: 80px; }

/* ═══════ 通用 ═══════ */
.status-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.status-chip { font-size: 13px; padding: 3px 12px; border-radius: 12px; background: #eceff1; color: #555; }
.status-chip.done { background: #e8f5e9; color: #2e7d32; }
.status-chip.failed { background: #ffebee; color: #c62828; }
.status-chip.running, .status-chip.pending { background: #fff3e0; color: #e65100; }
.meta-item { font-size: 12px; color: #999; }
.card-arrow { color: #999; font-size: 12px; }

.fail-card { background: #ffebee; color: #c62828; border-radius: 8px; padding: 14px 18px; font-size: 13px; }
.pending-card { background: #fff; border-radius: 8px; padding: 24px; text-align: center; color: #e65100; font-size: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(230,81,0,.25); border-top-color: #e65100; border-radius: 50%; animation: spin .7s linear infinite; vertical-align: -2px; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
.empty { text-align: center; color: #999; font-size: 13px; padding: 30px 0; background: #fff; border-radius: 8px; }

/* 原始 JSON 卡片（底部，两平台通用） */
.raw-json-card { background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow: hidden; }
.raw-json-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; cursor: pointer; user-select: none; }
.raw-json-head:hover { background: #fafafa; }
.raw-json-title { font-size: 14px; font-weight: 600; color: #333; }
.raw-json-sub { font-size: 12px; color: #999; flex: 1; }
.raw-copy-btn { background: #f0f2f5; border: none; border-radius: 6px; padding: 4px 12px; font-size: 12px; color: #5470c6; cursor: pointer; }
.raw-copy-btn:hover { background: #e3e9f5; }
.raw-json-body { border-top: 1px solid #f0f0f0; padding: 12px 16px; max-height: 480px; overflow: auto; }
.raw-json-body .json-viewer { font-size: 12px; }
.raw-empty { color: #999; font-size: 13px; padding: 8px 0; }

/* AI 摘要等内联文本块（kv/table 通用区块样式在 TiSectionBody.vue 内） */
.sec-text { margin: 0; padding: 14px 16px; font-size: 13px; line-height: 1.7; color: #444; white-space: pre-wrap; word-break: break-all; font-family: inherit; }
</style>
