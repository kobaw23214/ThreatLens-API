<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { authFetch } from '../utils/auth.js'
import { renderMarkdown, extractToc } from '../utils/markdown.js'

const groups = ref([])          // [{key,title,items:[{key,title}]}]
const activeKey = ref('')
const activeTitle = ref('')
const rawContent = ref('')
const loadingList = ref(false)
const loadingDoc = ref(false)
const errorMsg = ref('')
const contentRef = ref(null)

// 已加载手册内容缓存：key -> {title, content(markdown 源)}
const cache = {}

const html = computed(() => rawContent.value ? renderMarkdown(rawContent.value) : '')
const toc = computed(() => rawContent.value ? extractToc(rawContent.value) : [])

// 取分组结构里的第一个手册条目(兼容 items / subgroups 两种形态)
function firstItemOf(grps) {
  for (const g of grps || []) {
    if (g.items?.length) return g.items[0]
    for (const sg of g.subgroups || []) {
      if (sg.items?.length) return sg.items[0]
    }
  }
  return null
}

async function loadManualList() {
  loadingList.value = true
  errorMsg.value = ''
  try {
    const res = await authFetch('/manuals')
    if (!res.ok) throw new Error(`加载手册列表失败 (HTTP ${res.status})`)
    const data = await res.json()
    groups.value = data.groups || []
    // 默认选中第一个手册(兼容有/无子分组两种结构)
    const first = firstItemOf(groups.value)
    if (first) await selectManual(first.key)
  } catch (e) {
    errorMsg.value = e.message || '加载手册列表失败'
  } finally {
    loadingList.value = false
  }
}

async function selectManual(key) {
  activeKey.value = key
  if (cache[key] !== undefined) {
    activeTitle.value = cache[key].title
    rawContent.value = cache[key].content
    await scrollTop()
    return
  }
  loadingDoc.value = true
  errorMsg.value = ''
  try {
    const res = await authFetch(`/manuals/${encodeURIComponent(key)}`)
    if (!res.ok) throw new Error(`加载手册失败 (HTTP ${res.status})`)
    const data = await res.json()
    cache[key] = { title: data.title || key, content: data.content || '' }
    activeTitle.value = cache[key].title
    rawContent.value = cache[key].content
    await scrollTop()
  } catch (e) {
    errorMsg.value = e.message || '加载手册失败'
    rawContent.value = ''
  } finally {
    loadingDoc.value = false
  }
}

// 把当前手册的 Markdown 原文(即 /manuals/{key} 返回的源文件内容)打包下载
function downloadManual() {
  if (!rawContent.value) return
  const blob = new Blob([rawContent.value], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${activeTitle.value || activeKey.value}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

async function scrollTop() {
  await nextTick()
  contentRef.value?.scrollTo({ top: 0 })
}

function scrollToHeading(id) {
  const el = contentRef.value?.querySelector(`#${CSS.escape(id)}`)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// 拦截 markdown 正文内的锚点链接点击（如目录里的 [概述](#概述)）。
// 不拦截的话，点击会修改 location.hash，触发 App.vue 的 hashchange 监听，
// 把页面错误地切回 queue 标签。这里改为阻止默认行为 + 手动平滑滚动。
function onContentClick(e) {
  const a = e.target.closest('a')
  if (!a) return
  const href = a.getAttribute('href') || ''
  if (!href.startsWith('#')) return   // 外链/普通链接放行
  e.preventDefault()
  // href 可能是 URL 编码的中文锚点，需解码后再匹配元素 id
  let id = href.slice(1)
  try { id = decodeURIComponent(id) } catch (_) { /* 保持原值 */ }
  scrollToHeading(id)
}

onMounted(loadManualList)
</script>

<template>
  <div class="manual-viewer">
    <!-- 左侧：手册列表 -->
    <aside class="manual-sidebar">
      <div v-if="loadingList" class="side-hint">加载中…</div>
      <div v-for="group in groups" :key="group.key" class="manual-group">
        <div class="group-title">{{ group.title }}</div>

        <!-- 无子分组:条目直接挂在组下 -->
        <template v-if="group.items">
          <button
            v-for="item in group.items"
            :key="item.key"
            class="manual-item"
            :class="{ active: activeKey === item.key }"
            @click="selectManual(item.key)"
          >
            <svg class="doc-icon" viewBox="0 0 16 16">
              <path d="M4 2h5l3 3v9a1 1 0 01-1 1H4a1 1 0 01-1-1V3a1 1 0 011-1z"
                    fill="none" stroke="currentColor" stroke-width="1.2"/>
              <path d="M9 2v3h3" fill="none" stroke="currentColor" stroke-width="1.2"/>
            </svg>
            <span>{{ item.title }}</span>
          </button>
        </template>

        <!-- 有子分组(如 模型手册 → NER / ATT&CK):二级标题 + 三级条目 -->
        <template v-else-if="group.subgroups">
          <div v-for="sg in group.subgroups" :key="sg.key" class="manual-subgroup">
            <div class="subgroup-title">{{ sg.title }}</div>
            <button
              v-for="item in sg.items"
              :key="item.key"
              class="manual-item nested"
              :class="{ active: activeKey === item.key }"
              @click="selectManual(item.key)"
            >
              <svg class="doc-icon" viewBox="0 0 16 16">
                <path d="M4 2h5l3 3v9a1 1 0 01-1 1H4a1 1 0 01-1-1V3a1 1 0 011-1z"
                      fill="none" stroke="currentColor" stroke-width="1.2"/>
                <path d="M9 2v3h3" fill="none" stroke="currentColor" stroke-width="1.2"/>
              </svg>
              <span>{{ item.title }}</span>
            </button>
          </div>
        </template>
      </div>
    </aside>

    <!-- 中间：工具栏(固定不滚动) + Markdown 渲染 -->
    <div class="manual-main">
      <div class="doc-toolbar">
        <button
          class="download-btn"
          type="button"
          :disabled="!rawContent || loadingDoc"
          title="下载当前手册的 Markdown 源文件"
          @click="downloadManual"
        >
          <svg class="dl-icon" viewBox="0 0 16 16">
            <path d="M8 2.5v7m0 0L5.2 6.7M8 9.5l2.8-2.8" fill="none" stroke="currentColor"
                  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M3 12.8h10" fill="none" stroke="currentColor"
                  stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          下载手册
        </button>
      </div>
      <div ref="contentRef" class="manual-content" @click="onContentClick">
        <div v-if="errorMsg" class="doc-error">{{ errorMsg }}</div>
        <div v-else-if="loadingDoc" class="doc-loading">加载手册中…</div>
        <article v-else class="markdown-body" v-html="html"></article>
      </div>
    </div>

    <!-- 右侧：目录 TOC -->
    <aside class="manual-toc" v-if="toc.length && !loadingDoc">
      <div class="toc-title">目录</div>
      <ul>
        <li
          v-for="(h, i) in toc"
          :key="i"
          :class="`toc-l${h.level}`"
          @click="scrollToHeading(h.id)"
        >{{ h.text }}</li>
      </ul>
    </aside>
  </div>
</template>

<style scoped>
.manual-viewer {
  display: grid;
  grid-template-columns: 220px 1fr 220px;
  gap: 16px;
  height: calc(100vh - 56px - 32px);
}

/* ── 左侧手册列表 ── */
.manual-sidebar {
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  padding: 12px 8px;
  overflow-y: auto;
}
.side-hint { font-size: 13px; color: #999; padding: 10px; }
.manual-group { margin-bottom: 16px; }
.group-title {
  font-size: 12px;
  font-weight: 600;
  color: #999;
  text-transform: uppercase;
  letter-spacing: .5px;
  padding: 6px 10px;
}
.manual-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  background: transparent;
  border: none;
  text-align: left;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #444;
  transition: all .15s;
}
.manual-item:hover { background: #f4f6fa; color: #1a1a2e; }
.manual-item.active { background: #eef1fb; color: #5470c6; font-weight: 600; }
.doc-icon { width: 15px; height: 15px; flex-shrink: 0; }

/* ── 二级分组(NER / ATT&CK)── */
.manual-subgroup { margin: 4px 0 8px; }
.subgroup-title {
  font-size: 11.5px;
  font-weight: 600;
  color: #5470c6;
  padding: 4px 10px 2px;
  letter-spacing: .3px;
}
.manual-item.nested { padding-left: 22px; font-size: 12.5px; }

/* ── 中间内容(工具栏 + 滚动区) ── */
.manual-main {
  display: flex;
  flex-direction: column;
  min-height: 0;                 /* 让内部滚动区在 grid 行内正确收缩 */
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  overflow: hidden;              /* 保持圆角 */
}
.manual-content {
  flex: 1;
  min-height: 0;
  padding: 24px 40px 32px;
  overflow-y: auto;
}
.doc-loading, .doc-error { color: #999; font-size: 14px; padding: 20px 0; }
.doc-error { color: #c0392b; }

/* ── 顶部固定工具栏(在滚动区之外，不遮挡正文) ── */
.doc-toolbar {
  flex-shrink: 0;
  display: flex;
  justify-content: flex-end;
  padding: 10px 20px;
  border-bottom: 1px solid #f0f1f4;
}
.download-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: #5470c6;
  color: #fff;
  border: 1px solid #4a63b3;
  border-radius: 4px;
  padding: 4px 12px;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: all .2s;
  white-space: nowrap;
}
.download-btn:hover { background: #4a63b3; }
.download-btn:disabled { opacity: .5; cursor: default; }
.dl-icon { width: 14px; height: 14px; flex-shrink: 0; }

/* ── 右侧目录 ── */
.manual-toc {
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  padding: 16px 12px;
  overflow-y: auto;
}
.toc-title {
  font-size: 12px;
  font-weight: 600;
  color: #999;
  text-transform: uppercase;
  letter-spacing: .5px;
  margin-bottom: 8px;
  padding: 0 6px;
}
.manual-toc ul { list-style: none; }
.manual-toc li {
  font-size: 12.5px;
  color: #666;
  padding: 4px 6px;
  cursor: pointer;
  border-radius: 4px;
  line-height: 1.4;
  transition: all .12s;
  border-left: 2px solid transparent;
}
.manual-toc li:hover { color: #5470c6; background: #f4f6fa; }
.toc-l1 { font-weight: 600; color: #333; }
.toc-l2 { padding-left: 14px; }
.toc-l3 { padding-left: 24px; font-size: 12px; }
.toc-l4 { padding-left: 34px; font-size: 11.5px; color: #888; }

@media (max-width: 1100px) {
  .manual-viewer { grid-template-columns: 200px 1fr; }
  .manual-toc { display: none; }
}
@media (max-width: 760px) {
  .manual-viewer { grid-template-columns: 1fr; height: auto; }
  .manual-sidebar { height: auto; }
}
</style>

<!-- markdown-body 样式不能 scoped（v-html 内容） -->
<style>
.markdown-body {
  color: #24292f;
  font-size: 14.5px;
  line-height: 1.7;
  word-wrap: break-word;
}
.markdown-body h1 { font-size: 26px; padding-bottom: 10px; border-bottom: 1px solid #e1e4e8; margin: 8px 0 18px; }
.markdown-body h2 { font-size: 21px; padding-bottom: 8px; border-bottom: 1px solid #eaecef; margin: 28px 0 16px; }
.markdown-body h3 { font-size: 17px; margin: 22px 0 12px; }
.markdown-body h4 { font-size: 15px; margin: 18px 0 10px; }
.markdown-body p { margin: 10px 0; }
.markdown-body a { color: #2f6feb; text-decoration: none; }
.markdown-body a:hover { text-decoration: underline; }
.markdown-body blockquote {
  margin: 12px 0;
  padding: 8px 16px;
  color: #57606a;
  border-left: 4px solid #d0d7de;
  background: #f6f8fa;
  border-radius: 0 6px 6px 0;
}
.markdown-body ul, .markdown-body ol { padding-left: 26px; margin: 10px 0; }
.markdown-body li { margin: 4px 0; }
.markdown-body code {
  background: rgba(175,184,193,.2);
  padding: .2em .4em;
  border-radius: 4px;
  font-size: 85%;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}
.markdown-body pre.hljs {
  background: #f6f8fa;
  padding: 14px 16px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 12px 0;
  border: 1px solid #e1e4e8;
}
.markdown-body pre.hljs code {
  background: transparent;
  padding: 0;
  font-size: 13px;
  line-height: 1.5;
}
.markdown-body table {
  border-collapse: collapse;
  margin: 14px 0;
  display: block;
  overflow-x: auto;
  width: 100%;
}
.markdown-body th, .markdown-body td {
  border: 1px solid #d0d7de;
  padding: 7px 13px;
  text-align: left;
  font-size: 13.5px;
}
.markdown-body th { background: #f6f8fa; font-weight: 600; }
.markdown-body tr:nth-child(2n) { background: #f9fafb; }
.markdown-body hr { border: none; border-top: 1px solid #e1e4e8; margin: 24px 0; }
.markdown-body img { max-width: 100%; }
.markdown-body .header-anchor {
  color: #d0d7de;
  text-decoration: none;
  margin-left: 8px;
  opacity: 0;
  font-weight: 400;
  font-size: 0.85em;
  transition: opacity 0.15s;
}
.markdown-body h1:hover .header-anchor,
.markdown-body h2:hover .header-anchor,
.markdown-body h3:hover .header-anchor,
.markdown-body h4:hover .header-anchor { opacity: 0.6; }
.markdown-body .header-anchor:hover { opacity: 1; color: #5470c6; }
</style>
