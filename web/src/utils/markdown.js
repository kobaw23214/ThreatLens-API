// markdown-it 渲染器配置（语法高亮 + 标题锚点 + 安全默认）
import MarkdownIt from 'markdown-it'
import anchor from 'markdown-it-anchor'
import hljs from 'highlight.js/lib/common'

// slugify：把中文/英文标题转成可用作 id 的锚点，供目录跳转
function slugify(s) {
  return String(s)
    .trim()
    .toLowerCase()
    .replace(/[\s]+/g, '-')
    // 保留中文、字母、数字、连字符，其余去掉
    .replace(/[^\u4e00-\u9fa5a-z0-9\-]/g, '')
}

const md = new MarkdownIt({
  html: true,         // 手册源均为仓库内可信文件，允许表格等受控 HTML
  linkify: true,      // 自动把纯文本 URL 转为链接
  breaks: false,
  highlight(code, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return '<pre class="hljs"><code>' +
          hljs.highlight(code, { language: lang, ignoreIllegals: true }).value +
          '</code></pre>'
      } catch (_) { /* fallthrough */ }
    }
    return '<pre class="hljs"><code>' + md.utils.escapeHtml(code) + '</code></pre>'
  },
})

md.use(anchor, {
  slugify,
  permalink: anchor.permalink.linkInsideHeader({
    symbol: '#',
    placement: 'after',
    ariaHidden: true,
    class: 'header-anchor',
  }),
  level: [1, 2, 3, 4],
})

// 外部链接新开标签页
const defaultLinkOpen = md.renderer.rules.link_open ||
  function (tokens, idx, options, env, self) { return self.renderToken(tokens, idx, options) }
md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
  const href = tokens[idx].attrGet('href') || ''
  if (/^https?:\/\//.test(href)) {
    tokens[idx].attrSet('target', '_blank')
    tokens[idx].attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, idx, options, env, self)
}

export function renderMarkdown(src) {
  return md.render(src || '')
}

// 从 Markdown 源抽取标题，生成右侧/顶部目录（TOC）
export function extractToc(src) {
  const lines = (src || '').split('\n')
  const toc = []
  let inFence = false
  for (const line of lines) {
    if (/^```/.test(line.trim())) { inFence = !inFence; continue }
    if (inFence) continue
    const m = /^(#{1,4})\s+(.*)$/.exec(line)
    if (m) {
      const level = m[1].length
      const text = m[2].replace(/\s*#+\s*$/, '').trim()
      toc.push({ level, text, id: slugify(text) })
    }
  }
  return toc
}

export { slugify }
