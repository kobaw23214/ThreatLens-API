<script setup>
/**
 * 联网情报通用区块渲染器：kv（键值网格）/ table（表格）/ text（原文）三种格式。
 */
defineProps({
  section: { type: Object, required: true },
})

function kvText(v) {
  if (Array.isArray(v)) return v.join('、')
  if (v === null || v === undefined || v === '') return '—'
  return String(v)
}
</script>

<template>
  <div v-if="section.format === 'kv'" class="kv-grid">
    <div v-for="(v, k) in (section.items || {})" :key="k" class="kv-item">
      <div class="kv-key">{{ k }}</div>
      <div class="kv-value">
        <!-- 数组值 → chip 列表（威胁标签/开放端口等平台 chip 场景） -->
        <template v-if="Array.isArray(v)">
          <span v-for="(t, i) in v" :key="i" class="kv-chip">{{ t }}</span>
        </template>
        <template v-else>{{ kvText(v) }}</template>
      </div>
    </div>
  </div>
  <table v-else-if="section.format === 'table'" class="sec-table">
    <thead>
      <tr><th v-for="(c, ci) in (section.columns || [])" :key="ci">{{ c }}</th></tr>
    </thead>
    <tbody>
      <tr v-for="(row, ri) in (section.rows || [])" :key="ri">
        <td v-for="(cell, ci) in row" :key="ci" class="mono">{{ kvText(cell) }}</td>
      </tr>
    </tbody>
  </table>
  <pre v-else class="sec-text">{{ section.content }}</pre>
</template>

<style scoped>
.kv-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.kv-item { display: flex; gap: 10px; padding: 10px 16px; border-bottom: 1px solid #f5f5f5; align-items: baseline; }
.kv-key { font-size: 12px; color: #999; white-space: nowrap; flex-shrink: 0; min-width: 64px; }
.kv-value { font-size: 13px; color: #333; word-break: break-all; }
.kv-chip { display: inline-block; font-size: 12px; background: #f0f2f5; border: 1px solid #e0e3e8; color: #555; border-radius: 4px; padding: 1px 8px; margin: 1px 4px 1px 0; }
.sec-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.sec-table th, .sec-table td { border-bottom: 1px solid #f0f0f0; padding: 8px 14px; text-align: left; vertical-align: top; }
.sec-table td { white-space: pre-wrap; }
.sec-table th { background: #fafafa; color: #666; font-weight: 600; font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.sec-text { margin: 0; padding: 14px 16px; font-size: 13px; line-height: 1.7; color: #444; white-space: pre-wrap; word-break: break-all; font-family: inherit; }
</style>
