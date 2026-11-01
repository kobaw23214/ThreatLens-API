<script setup>
/**
 * 实体详情弹框 (long-text-chunking-ner)
 *
 * 点击实体详情表中的"标准标签"单元格时弹出。
 *
 * 功能:
 *   - 顶部显示实体文本 + 标签徽章,色带对齐所属 chunk 色
 *   - 元数据网格: 标准/原始标签、字符位置、置信度、所属 Chunk、全局序号
 *   - 原文定位: 默认以"实体前后 300 字符"为上下文窗口,按 Chunk 分段展示
 *   - 当前实体在原文中用红色下划线 + 标签上标突出显示
 *   - 每个 Chunk 以色带 header 标注,当前实体所在 Chunk 显眼地打 "⬤ 当前实体所在"
 *   - 可切换到"完整原文"模式,前后文省略数字动态计算
 *   - ESC / 点击遮罩 / 点击关闭按钮 → 关闭
 *   - 点击"跳转到 Chunk" → emit goto-chunk,让父组件定位到 Chunk 明细 + 实体分页
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { colorFor } from '../utils/chunkColors.js'

const props = defineProps({
  entity:      { type: Object, required: true },
  inputText:   { type: String, default: '' },
  chunks:      { type: Array,  default: () => [] },
  allEntities: { type: Array,  default: () => [] },
  globalIndex: { type: Number, default: -1 },
})

const emit = defineEmits(['close', 'goto-chunk'])

const CONTEXT_WINDOW = 600            // 上下文窗口总长度(实体前后各 300)
const fullView = ref(false)

function close() {
  emit('close')
}

function onKeydown(e) {
  if (e.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  document.body.style.overflow = 'hidden'
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})

// ─── 上下文窗口计算 ──────────────────────────────────────────────────────

const contextRange = computed(() => {
  const txt = props.inputText
  const e = props.entity
  if (!txt || !e) return { start: 0, end: 0 }
  if (fullView.value) return { start: 0, end: txt.length }
  const half = CONTEXT_WINDOW / 2
  return {
    start: Math.max(0, e.start - half),
    end:   Math.min(txt.length, e.end + half),
  }
})

const hiddenBefore = computed(() => contextRange.value.start)
const hiddenAfter = computed(() => {
  const txt = props.inputText || ''
  return Math.max(0, txt.length - contextRange.value.end)
})

// 与窗口有交集的 chunk,按 chunk_index 升序
const visibleChunks = computed(() => {
  const { start, end } = contextRange.value
  return (props.chunks || [])
    .filter(ch => ch.char_start < end && ch.char_end > start)
    .sort((a, b) => a.chunk_index - b.chunk_index)
})

// 按 chunk 分段,每段内部再切分 "text / current-entity" 子部分
const chunkSegments = computed(() => {
  const txt = props.inputText
  const e = props.entity
  const { start: ctxStart, end: ctxEnd } = contextRange.value
  if (!txt || !e) return []

  const vc = visibleChunks.value
  if (vc.length === 0) {
    // 无 chunk 数据(未分块):整段作为单一块
    return [{
      chunk_index: null,
      chunk_char_start: 0,
      chunk_char_end: txt.length,
      display_start: ctxStart,
      display_end:   ctxEnd,
      leading_ellipsis:  false,
      trailing_ellipsis: false,
      is_current: true,
      parts: splitBody(txt, ctxStart, ctxEnd, e),
    }]
  }

  return vc.map(ch => {
    const dispStart = Math.max(ch.char_start, ctxStart)
    const dispEnd   = Math.min(ch.char_end, ctxEnd)
    return {
      chunk_index:       ch.chunk_index,
      chunk_char_start:  ch.char_start,
      chunk_char_end:    ch.char_end,
      display_start:     dispStart,
      display_end:       dispEnd,
      // chunk 内容被窗口截断了左/右侧,渲染省略号提示
      leading_ellipsis:  dispStart > ch.char_start,
      trailing_ellipsis: dispEnd < ch.char_end,
      is_current:        ch.chunk_index === e.chunk_index,
      parts: splitBody(txt, dispStart, dispEnd, e),
    }
  })
})

function splitBody(txt, from, to, entity) {
  // 将 [from, to) 的文本切成: text → current-entity → text
  const parts = []
  const es = entity.start
  const ee = entity.end

  if (ee <= from || es >= to) {
    // 实体不落在当前窗口段 → 纯文本
    parts.push({ kind: 'text', text: txt.slice(from, to) })
    return parts
  }

  const hlStart = Math.max(es, from)
  const hlEnd   = Math.min(ee, to)

  if (hlStart > from) parts.push({ kind: 'text',    text: txt.slice(from, hlStart) })
  parts.push({ kind: 'current', text: txt.slice(hlStart, hlEnd) })
  if (hlEnd < to)     parts.push({ kind: 'text',    text: txt.slice(hlEnd, to) })
  return parts
}

function gotoChunk() {
  if (props.entity.chunk_index != null) {
    emit('goto-chunk', props.entity.chunk_index)
    close()
  }
}

function toggleFullView() {
  fullView.value = !fullView.value
}
</script>

<template>
  <Teleport to="body">
    <div class="em-overlay" @click.self="close">
      <div class="em-modal" role="dialog" aria-modal="true" @click.stop>
        <!-- ═══ Header ═══ -->
        <header
          class="em-header"
          :style="{ borderTopColor: colorFor(entity.chunk_index) }"
        >
          <div class="em-header-main">
            <div class="em-eyebrow">实体详情</div>
            <div class="em-title">
              <span class="em-entity-text">{{ entity.text }}</span>
              <span class="em-label-badge">{{ entity.label }}</span>
            </div>
          </div>
          <button class="em-close" @click="close" aria-label="关闭">
            <svg viewBox="0 0 20 20" width="18" height="18">
              <path d="M5 5 L15 15 M15 5 L5 15"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </button>
        </header>

        <!-- ═══ 元数据 ═══ -->
        <section class="em-meta">
          <div class="meta-grid">
            <div class="meta-item">
              <span class="meta-k">标准标签</span>
              <span class="meta-v"><span class="tag-badge">{{ entity.label }}</span></span>
            </div>
            <div class="meta-item" v-if="entity.raw_label && entity.raw_label !== entity.label">
              <span class="meta-k">原始标签</span>
              <span class="meta-v"><span class="tag-badge raw">{{ entity.raw_label }}</span></span>
            </div>
            <div class="meta-item">
              <span class="meta-k">字符位置</span>
              <span class="meta-v mono">
                {{ entity.start }} – {{ entity.end }}
                <small>(len={{ entity.end - entity.start }})</small>
              </span>
            </div>
            <div class="meta-item">
              <span class="meta-k">置信度</span>
              <span
                class="meta-v score"
                :class="{
                  high: entity.score >= 0.8,
                  mid:  entity.score < 0.8 && entity.score >= 0.6,
                  low:  entity.score < 0.6,
                }"
              >{{ (entity.score * 100).toFixed(1) }}%</span>
            </div>
            <div class="meta-item" v-if="entity.chunk_index != null">
              <span class="meta-k">所属 Chunk</span>
              <span class="meta-v">
                <span class="chunk-ref" :style="{ backgroundColor: colorFor(entity.chunk_index) }">
                  #{{ entity.chunk_index }}
                </span>
                <span class="mono small">
                  [{{ entity.chunk_char_start }}, {{ entity.chunk_char_end }})
                </span>
              </span>
            </div>
            <div class="meta-item" v-if="globalIndex >= 0">
              <span class="meta-k">全局序号</span>
              <span class="meta-v mono">#{{ globalIndex + 1 }}</span>
            </div>
            <div class="meta-item" v-if="entity.model_name">
              <span class="meta-k">提取模型</span>
              <span class="meta-v">
                <span class="tag-badge model">{{ entity.model_name }}</span>
              </span>
            </div>
          </div>
        </section>

        <!-- ═══ 原文定位 ═══ -->
        <section class="em-section">
          <div class="em-section-header">
            <div class="em-section-title">
              <span class="icon">📍</span>
              原文定位
              <span class="em-section-sub">
                <template v-if="!fullView">
                  (上下文窗口 · 前后 {{ CONTEXT_WINDOW / 2 }} 字符)
                </template>
                <template v-else>(完整原文)</template>
              </span>
            </div>
            <button class="em-toggle-btn" @click="toggleFullView">
              {{ fullView ? '收起到上下文' : '显示完整原文' }}
            </button>
          </div>

          <div class="em-passage">
            <!-- 窗口前省略 -->
            <div v-if="hiddenBefore > 0" class="em-ellipsis">
              <span class="dots">⋯</span>
              <span>前文省略 {{ hiddenBefore }} 字符</span>
            </div>

            <!-- 按 chunk 分段 -->
            <template v-for="(seg, si) in chunkSegments" :key="si">
              <div
                v-if="seg.chunk_index != null"
                class="em-chunk-header"
                :style="{ backgroundColor: colorFor(seg.chunk_index) }"
              >
                <span class="chunk-label">
                  <span class="chunk-badge">Chunk #{{ seg.chunk_index }}</span>
                  <span class="chunk-range mono">
                    [{{ seg.chunk_char_start }}, {{ seg.chunk_char_end }})
                  </span>
                </span>
                <span v-if="seg.is_current" class="em-current-tag">⬤ 当前实体所在</span>
              </div>

              <div class="em-chunk-body" :class="{ 'is-current': seg.is_current }">
                <span v-if="seg.leading_ellipsis" class="em-inline-ellipsis">⋯</span>
                <template v-for="(part, pi) in seg.parts" :key="pi">
                  <span v-if="part.kind === 'text'" class="em-text">{{ part.text }}</span>
                  <mark
                    v-else-if="part.kind === 'current'"
                    class="em-highlight"
                    :title="`${entity.label} · [${entity.start}, ${entity.end}) · ${(entity.score * 100).toFixed(1)}%`"
                  >
                    <span class="em-hl-text">{{ part.text }}</span>
                    <span class="em-hl-marker">{{ entity.label }}</span>
                  </mark>
                </template>
                <span v-if="seg.trailing_ellipsis" class="em-inline-ellipsis">⋯</span>
              </div>
            </template>

            <!-- 窗口后省略 -->
            <div v-if="hiddenAfter > 0" class="em-ellipsis">
              <span class="dots">⋯</span>
              <span>后文省略 {{ hiddenAfter }} 字符</span>
            </div>
          </div>
        </section>

        <!-- ═══ Footer ═══ -->
        <footer class="em-footer">
          <span class="em-hint">按 ESC 或点击背景关闭</span>
          <div class="em-actions">
            <button
              v-if="entity.chunk_index != null"
              class="em-btn-secondary"
              @click="gotoChunk"
            >
              跳转到 Chunk #{{ entity.chunk_index }}
            </button>
            <button class="em-btn-primary" @click="close">关闭</button>
          </div>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.em-overlay {
  position: fixed;
  inset: 0;
  background: rgba(17, 24, 39, 0.55);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  animation: em-fade 0.18s ease;
}
@keyframes em-fade { from { opacity: 0 } to { opacity: 1 } }

.em-modal {
  background: #fff;
  border-radius: 12px;
  width: 100%;
  max-width: 820px;
  max-height: calc(100vh - 48px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 24px 48px rgba(0, 0, 0, 0.24), 0 0 0 1px rgba(0, 0, 0, 0.06);
  border-top: 4px solid #5470c6;
  animation: em-slide 0.24s cubic-bezier(0.22, 1, 0.36, 1);
}
@keyframes em-slide {
  from { opacity: 0; transform: translateY(12px) }
  to   { opacity: 1; transform: translateY(0) }
}

/* ── Header ── */
.em-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 18px 22px 14px;
  border-bottom: 1px solid #f0f2f5;
  background: linear-gradient(to bottom, #fafbff 0%, #fff 100%);
}
.em-header-main { flex: 1; min-width: 0; }
.em-eyebrow {
  font-size: 11px;
  color: #9ca3af;
  letter-spacing: 0.8px;
  text-transform: uppercase;
  font-weight: 600;
  margin-bottom: 4px;
}
.em-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.em-entity-text {
  font-size: 22px;
  font-weight: 700;
  color: #111827;
  letter-spacing: -0.2px;
  word-break: break-all;
}
.em-label-badge {
  display: inline-flex;
  padding: 3px 10px;
  background: #e0e7ff;
  color: #3730a3;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.3px;
  flex-shrink: 0;
}
.em-close {
  background: none;
  border: none;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  color: #6b7280;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
  flex-shrink: 0;
}
.em-close:hover {
  background: #f3f4f6;
  color: #111827;
}

/* ── 元数据 ── */
.em-meta {
  padding: 14px 22px;
  background: #fafbff;
  border-bottom: 1px solid #f0f2f5;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px 24px;
}
.meta-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.meta-k {
  font-size: 11px;
  color: #6b7280;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.meta-v {
  font-size: 13px;
  color: #111827;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.meta-v.mono {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.meta-v small { color: #9ca3af; font-size: 11px; }

.tag-badge {
  padding: 2px 8px;
  background: #e0e7ff;
  color: #3730a3;
  border-radius: 6px;
  font-weight: 600;
  font-size: 12px;
}
.tag-badge.raw { background: #fef3c7; color: #92400e; }
.tag-badge.model { background: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; }

.chunk-ref {
  padding: 2px 8px;
  border-radius: 10px;
  color: #111827;
  font-weight: 600;
  font-size: 12px;
  border: 1px solid rgba(0, 0, 0, .08);
}
.meta-v .small { color: #6b7280; font-size: 11px; }

.score {
  font-weight: 700;
  font-family: 'SFMono-Regular', Consolas, monospace;
}
.score.high { color: #047857; }
.score.mid  { color: #c2410c; }
.score.low  { color: #b91c1c; }

/* ── 原文定位 ── */
.em-section {
  padding: 16px 22px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}
.em-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}
.em-section-title {
  font-size: 13px;
  font-weight: 700;
  color: #374151;
}
.em-section-title .icon { margin-right: 4px; }
.em-section-sub {
  font-weight: 400;
  color: #6b7280;
  font-size: 12px;
  margin-left: 4px;
}
.em-toggle-btn {
  background: #eef2ff;
  color: #3556a3;
  border: 1px solid #c7d2fe;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.em-toggle-btn:hover {
  background: #e0e7ff;
  border-color: #a5b4fc;
}

.em-passage {
  background: #f9fafb;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
}

/* 窗口省略提示 */
.em-ellipsis {
  padding: 8px 14px;
  font-size: 12px;
  color: #6b7280;
  background: #f3f4f6;
  display: flex;
  align-items: center;
  gap: 8px;
  font-style: italic;
}
.em-ellipsis + .em-chunk-header,
.em-chunk-body + .em-ellipsis {
  border-top: 1px dashed #e5e7eb;
}
.em-ellipsis .dots {
  color: #6b7280;
  font-size: 16px;
  font-style: normal;
}

/* Chunk 色带 header */
.em-chunk-header {
  padding: 6px 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  border-bottom: 1px solid rgba(0, 0, 0, .08);
  gap: 10px;
  position: sticky;
  top: 0;
  z-index: 1;
}
.chunk-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.chunk-badge {
  background: rgba(255, 255, 255, .9);
  color: #111827;
  padding: 1px 8px;
  border-radius: 10px;
  font-weight: 700;
  font-size: 11px;
  border: 1px solid rgba(0, 0, 0, .08);
}
.chunk-range {
  color: #374151;
  font-size: 11px;
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.em-current-tag {
  color: #b91c1c;
  font-weight: 700;
  font-size: 11px;
  background: rgba(255, 255, 255, .9);
  padding: 1px 8px;
  border-radius: 10px;
  letter-spacing: 0.3px;
  border: 1px solid rgba(220, 38, 38, .3);
}

/* Chunk 正文 */
.em-chunk-body {
  padding: 14px 16px;
  font-size: 14px;
  line-height: 1.9;
  color: #374151;
  white-space: pre-wrap;
  word-break: break-word;
  background: #fff;
}
.em-chunk-body + .em-chunk-header { margin-top: 0; }
.em-chunk-body.is-current { background: #fffbeb; }
.em-inline-ellipsis {
  color: #9ca3af;
  margin: 0 3px;
  font-weight: 700;
}
.em-text { color: #374151; }

/* 当前实体高亮 */
.em-highlight {
  background: rgba(220, 38, 38, 0.10);
  color: #991b1b;
  font-weight: 600;
  padding: 1px 2px;
  border-bottom: 2px solid #dc2626;
  border-radius: 2px 2px 0 0;
  cursor: help;
  animation: em-pulse 1.6s ease 1;
  position: relative;
  display: inline;
}
@keyframes em-pulse {
  0%   { background: rgba(220, 38, 38, 0.35); }
  60%  { background: rgba(220, 38, 38, 0.18); }
  100% { background: rgba(220, 38, 38, 0.10); }
}
.em-hl-marker {
  display: inline-block;
  font-size: 9px;
  font-weight: 700;
  padding: 0 4px;
  margin-left: 2px;
  border-radius: 3px;
  background: #dc2626;
  color: #fff;
  vertical-align: super;
  line-height: 12px;
  letter-spacing: 0.3px;
}

/* ── Footer ── */
.em-footer {
  padding: 14px 22px;
  border-top: 1px solid #f0f2f5;
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #fff;
  flex-shrink: 0;
  gap: 12px;
}
.em-hint { font-size: 12px; color: #9ca3af; }
.em-actions { display: flex; gap: 8px; }

.em-btn-secondary,
.em-btn-primary {
  padding: 7px 16px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
  border: none;
}
.em-btn-secondary {
  background: #fff;
  color: #374151;
  border: 1px solid #d1d5db;
}
.em-btn-secondary:hover {
  background: #f3f4f6;
  border-color: #9ca3af;
}
.em-btn-primary {
  background: #5470c6;
  color: #fff;
}
.em-btn-primary:hover { background: #4a63b3; }

@media (max-width: 680px) {
  .em-overlay { padding: 0; }
  .em-modal { max-height: 100vh; border-radius: 0; }
  .meta-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
