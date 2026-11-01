<script setup>
/**
 * 分块信息面板 (long-text-chunking-ner)
 *
 * 将"分块概览"与"Chunk 明细表"合并到同一个卡片中。
 *
 * props:
 *   chunkMetadata     chunk_metadata JSON (来自 /tasks/{id}/result);可为 null
 *   activeChunkIndex  外部控制高亮哪一行 (null = 无)
 *
 * emit:
 *   chunk-click(chunk_index)  用户点击 Chunk 行时触发,上报给父组件
 *
 * 渲染规则:
 *   - chunkMetadata=null              → "历史任务 / 未分块" 占位
 *   - chunking_enabled=false          → 仅显示 "本次未启用分块" 一行提示
 *   - chunking_enabled=true           → 6 个指标卡片 + Chunk 明细表
 *   - token_count >= effective * 0.95 → 橙色警示高亮
 *   - status === 'failed'             → 整行红底 + error tooltip
 *   - activeChunkIndex 变化           → 目标行高亮 + scrollIntoView
 *
 * 对应 Requirement 7.1-7.7 / 8.4。
 */
import { computed, ref, watch, nextTick } from 'vue'

const props = defineProps({
  chunkMetadata: {
    type: [Object, null],
    default: null,
  },
  activeChunkIndex: {
    type: [Number, null],
    default: null,
  },
})

const emit = defineEmits(['chunk-click'])

const hasMetadata = computed(() => props.chunkMetadata != null)
const chunkingOn  = computed(() => !!props.chunkMetadata?.chunking_enabled)
const chunks      = computed(() => props.chunkMetadata?.chunks ?? [])

function fmt(val, suffix = '') {
  if (val == null) return '—'
  return String(val) + suffix
}

const items = computed(() => {
  if (!hasMetadata.value) return []
  const m = props.chunkMetadata
  return [
    { key: 'chunk_count',          label: '分块数',    value: fmt(m.chunk_count),                   tone: 'primary' },
    { key: 'model_max_tokens',     label: '模型上限',  value: fmt(m.model_max_tokens, ' tok'),      tone: 'neutral' },
    { key: 'effective_max_tokens', label: '有效上限',  value: fmt(m.effective_max_tokens, ' tok'),  tone: 'neutral' },
    { key: 'overlap',              label: '重叠',      value: fmt(m.overlap, ' 字符'),              tone: 'neutral' },
    { key: 'threshold',            label: 'threshold', value: fmt(m.threshold),                    tone: 'neutral' },
    {
      key: 'chunking_enabled',
      label: '分块状态',
      value: m.chunking_enabled ? '已启用' : '未启用',
      tone:  m.chunking_enabled ? 'success' : 'muted',
    },
  ]
})

// ─── 明细表交互 ──────────────────────────────────────────────────────────

const WARNING_RATIO = 0.95
const tbodyRef = ref(null)

function isNearLimit(ch) {
  const eff = props.chunkMetadata?.effective_max_tokens
  if (ch.token_count == null || !eff) return false
  return ch.token_count >= eff * WARNING_RATIO
}

function onRowClick(ch) {
  emit('chunk-click', ch.chunk_index)
}

// activeChunkIndex 变化 → 滚动到对应行并高亮
watch(
  () => props.activeChunkIndex,
  async (ci) => {
    if (ci == null || !tbodyRef.value) return
    await nextTick()
    const row = tbodyRef.value.querySelector(`[data-chunk-index="${ci}"]`)
    if (row?.scrollIntoView) {
      row.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  },
)
</script>

<template>
  <div class="chunk-panel">
    <div class="panel-header">
      <h3>📦 分块信息</h3>
      <span v-if="hasMetadata && chunkingOn" class="panel-sub">
        effective = model_max − labels − safety_margin
      </span>
    </div>

    <!-- 场景 1: 无 metadata (历史任务) -->
    <div v-if="!hasMetadata" class="placeholder legacy">
      <span class="placeholder-icon">ⓘ</span>
      <span>历史任务 / 未分块(后端无 chunk_metadata 记录)</span>
    </div>

    <!-- 场景 2: metadata 存在但未启用分块 -->
    <div v-else-if="!chunkingOn" class="placeholder disabled">
      <span class="placeholder-icon">●</span>
      <span>本次未启用分块(chunk_enabled=false 或模型 / 容量不支持)</span>
    </div>

    <!-- 场景 3: 启用分块 → 6 个指标卡片 + 明细表 -->
    <template v-else>
      <div class="overview-grid">
        <div
          v-for="it in items"
          :key="it.key"
          class="metric-card"
          :data-tone="it.tone"
        >
          <div class="metric-label">{{ it.label }}</div>
          <div class="metric-value">{{ it.value }}</div>
        </div>
      </div>

      <!-- 明细表 -->
      <div class="detail-subheader" v-if="chunks.length">
        <span class="subheader-title">📋 Chunk 明细</span>
        <span class="subheader-hint">
          {{ chunks.length }} 块 · 点击行可定位对应原文 / 实体
        </span>
      </div>

      <div v-if="chunks.length" class="table-wrap">
        <table class="chunk-table">
          <thead>
            <tr>
              <th>#</th>
              <th class="num">起始</th>
              <th class="num">结束</th>
              <th class="num">字符数</th>
              <th class="num">token 数</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody ref="tbodyRef">
            <tr
              v-for="ch in chunks"
              :key="ch.chunk_index"
              :data-chunk-index="ch.chunk_index"
              :class="{
                failed: ch.status === 'failed',
                active: ch.chunk_index === activeChunkIndex,
              }"
              :title="ch.status === 'failed' ? (ch.error || 'chunk 推理失败') : ''"
              @click="onRowClick(ch)"
            >
              <td class="idx">
                <span class="chunk-badge">#{{ ch.chunk_index }}</span>
              </td>
              <td class="num mono">{{ ch.char_start }}</td>
              <td class="num mono">{{ ch.char_end }}</td>
              <td class="num mono">{{ ch.char_length }}</td>
              <td
                class="num mono"
                :class="{ warning: isNearLimit(ch) }"
              >
                {{ ch.token_count ?? '—' }}
              </td>
              <td>
                <span class="status-pill" :data-status="ch.status">
                  {{ ch.status === 'failed' ? '失败' : '成功' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.chunk-panel {
  background: #fff;
  border-radius: 8px;
  padding: 16px 20px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, .08);
  margin-bottom: 16px;
}

.panel-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;
}

.panel-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: #333;
}

.panel-sub {
  font-size: 12px;
  color: #888;
  font-family: 'SFMono-Regular', Consolas, monospace;
}

/* 占位状态 */
.placeholder {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  border-radius: 6px;
  font-size: 13px;
}
.placeholder.legacy   { background: #f3f4f6; color: #6b7280; }
.placeholder.disabled { background: #fff7ed; color: #c2410c; }
.placeholder-icon { font-size: 14px; opacity: .85; }

/* 指标网格 */
.overview-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}

.metric-card {
  background: #f8f9fa;
  border-radius: 6px;
  padding: 12px;
  border-left: 3px solid #9ca3af;
}
.metric-card[data-tone='primary']  { border-left-color: #5470c6; }
.metric-card[data-tone='success']  { border-left-color: #3ba272; }
.metric-card[data-tone='muted']    { border-left-color: #9ca3af; }
.metric-card[data-tone='neutral']  { border-left-color: #6b7280; }

.metric-label {
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 4px;
  white-space: nowrap;
}

.metric-value {
  font-size: 18px;
  font-weight: 600;
  color: #111827;
  font-variant-numeric: tabular-nums;
}

/* ── 明细表 ── */
.detail-subheader {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 6px 0 8px;
  padding-top: 10px;
  border-top: 1px dashed #e5e7eb;
}
.subheader-title {
  font-size: 14px;
  font-weight: 600;
  color: #374151;
}
.subheader-hint {
  font-size: 12px;
  color: #888;
}

.table-wrap {
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid #e9ecef;
  border-radius: 6px;
}

.chunk-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.chunk-table thead th {
  position: sticky;
  top: 0;
  background: #f8f9fa;
  padding: 10px 12px;
  text-align: left;
  font-weight: 600;
  color: #4b5563;
  border-bottom: 1px solid #e5e7eb;
  font-size: 12px;
  letter-spacing: .2px;
}

.chunk-table tbody td {
  padding: 9px 12px;
  border-bottom: 1px solid #f1f3f5;
  color: #374151;
}

.chunk-table tbody tr {
  cursor: pointer;
  transition: background-color .12s;
}
.chunk-table tbody tr:hover          { background: #f8faff; }
.chunk-table tbody tr.active         { background: #eef4ff; box-shadow: inset 3px 0 0 #5470c6; }
.chunk-table tbody tr.failed         { background: #fef2f2; }
.chunk-table tbody tr.failed:hover   { background: #fee2e2; }
.chunk-table tbody tr.failed.active  { background: #fecaca; box-shadow: inset 3px 0 0 #dc2626; }

.num   { text-align: right; }
.mono  { font-family: 'SFMono-Regular', Consolas, monospace; font-variant-numeric: tabular-nums; }

.num.warning {
  color: #c2410c;
  font-weight: 700;
  background: #fff7ed;
  border-radius: 3px;
  padding: 2px 6px;
}

.idx { width: 56px; }
.chunk-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 28px;
  padding: 2px 8px;
  border-radius: 10px;
  background: #eef4ff;
  color: #3556a3;
  font-weight: 600;
  font-size: 12px;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 500;
}
.status-pill[data-status='done']   { background: #d1fae5; color: #065f46; }
.status-pill[data-status='failed'] { background: #fee2e2; color: #991b1b; }

@media (max-width: 1100px) {
  .overview-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 600px) {
  .overview-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
