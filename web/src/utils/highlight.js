/**
 * 调用台代码高亮（手写正则，零依赖）。
 *
 * - highlightJson：JSON 语法高亮，token 包 <span class="k|s|n|b|nl">
 *   （key / string / number / bool / null）。
 * - highlightCurl：curl 命令高亮，token 包 <span class="ck-*">：
 *     ck-c  命令(curl)      ck-m  HTTP 方法        ck-f  参数旗标(-X/-F/-H/-d/--...)
 *     ck-u  URL             ck-s  引号字符串       ck-ct 行尾续行符
 *     ck-hk -H 头部名（值仍按 ck-s 字符串着色）
 *   -d/--data 的请求体若以 { 或 [ 开头，内层按 highlightJson 着色。
 *
 * 输入均为各调用台自家 builder / API 响应（可信），输出经 v-html 注入，
 * 使用方 scoped 样式必须用 :deep 才能命中这些 span。
 */

/** JSON 语法高亮（key/string/number/bool/null） */
export function highlightJson(json) {
  if (!json) return ''
  const str = typeof json === 'string' ? json : JSON.stringify(json, null, 2)
  return str
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
      (m) => {
        let cls = 'n'
        if (/^"/.test(m)) cls = /:$/.test(m) ? 'k' : 's'
        else if (/true|false/.test(m)) cls = 'b'
        else if (/null/.test(m)) cls = 'nl'
        return `<span class="${cls}">${m}</span>`
      })
}

const _TOKEN_RE = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|https?:\/\/[^\s"'<>]+|\b(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b|--?[a-zA-Z][\w-]*|\bcurl\b|\\$)/gm
const _DATA_FLAGS = new Set(['-d', '--data', '--data-raw', '--data-binary', '--data-ascii', '--data-urlencode'])
const _HEADER_FLAGS = new Set(['-H', '--header'])

function _esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** curl 命令语法高亮（-H 按 KV、-d JSON 请求体按 JSON 着色） */
export function highlightCurl(text) {
  if (!text) return ''
  const raw = String(text)
  let out = ''
  let last = 0
  let prevFlag = null
  let m
  _TOKEN_RE.lastIndex = 0
  while ((m = _TOKEN_RE.exec(raw)) !== null) {
    out += _esc(raw.slice(last, m.index))  // token 间隔的空白/符号
    const tok = m[0]
    const ch = tok[0]
    if (ch === '"' || ch === "'") {
      const inner = tok.slice(1, -1)
      if (prevFlag && _DATA_FLAGS.has(prevFlag) && /^[{[]/.test(inner.trim())) {
        // -d '<json>' → 请求体按 JSON 高亮（highlightJson 内部自行转义）
        out += ch + highlightJson(inner) + ch
      } else if (prevFlag && _HEADER_FLAGS.has(prevFlag)) {
        // -H "Name: Value" → 头部名 / 值分开着色
        const ci = inner.indexOf(':')
        out += ci > 0
          ? `${ch}<span class="ck-hk">${_esc(inner.slice(0, ci))}</span>:<span class="ck-s">${_esc(inner.slice(ci + 1))}</span>${ch}`
          : `<span class="ck-s">${ch}${_esc(inner)}${ch}</span>`
      } else {
        out += `<span class="ck-s">${ch}${_esc(inner)}${ch}</span>`
      }
      prevFlag = null
    } else {
      let cls = 'm'
      if (ch === 'h') cls = 'u'
      else if (ch === '-') cls = 'f'
      else if (tok === 'curl') cls = 'c'
      else if (tok === '\\') cls = 'ct'
      out += `<span class="ck-${cls}">${_esc(tok)}</span>`
      prevFlag = cls === 'f' ? tok : null
    }
    last = m.index + tok.length
  }
  out += _esc(raw.slice(last))
  return out
}
