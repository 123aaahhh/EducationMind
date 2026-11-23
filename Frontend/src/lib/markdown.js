import { marked } from 'marked'
import DOMPurify from 'dompurify'
import katex from 'katex'
import hljs from 'highlight.js/lib/common'

const renderer = new marked.Renderer()

renderer.code = (code, infostring) => {
  const language = String(infostring || '').trim().split(/\s+/)[0]
  let body
  try {
    body = language && hljs.getLanguage(language)
      ? hljs.highlight(code, { language, ignoreIllegals: true }).value
      : hljs.highlightAuto(code).value
  } catch {
    body = escapeHtml(code)
  }
  const label = language ? escapeHtml(language) : 'text'
  return `<pre class="hljs"><code class="language-${label}">${body}</code></pre>`
}

marked.setOptions({ gfm: true, breaks: true })

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 先把公式替换为占位符，再做 Markdown 解析与安全过滤，
 * 最后注入 KaTeX 渲染结果（KaTeX 输出为可信 HTML）。
 */
function extractMath(source) {
  const store = []
  const push = (expr, display) => {
    store.push({ expr, display })
    return `%%KATEX${store.length - 1}%%`
  }
  let text = source.replace(/\$\$([\s\S]+?)\$\$/g, (_, expr) => push(expr, true))
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (_, expr) => push(expr, true))
  text = text.replace(/(?<!\\)\$([^\n$]+?)\$/g, (_, expr) => push(expr, false))
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (_, expr) => push(expr, false))
  return { text, store }
}

function renderTex(item) {
  if (!item) return ''
  try {
    return katex.renderToString(item.expr, {
      displayMode: item.display,
      throwOnError: false,
      strict: 'ignore',
      output: 'htmlAndMathml'
    })
  } catch {
    return escapeHtml(item.expr)
  }
}

export function renderMarkdown(source) {
  const input = String(source || '')
  if (!input.trim()) return ''
  const { text, store } = extractMath(input)
  const html = marked.parse(text, { renderer })
  const safe = DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['target', 'rel']
  })
  return safe
    .replace(/<p>\s*%%KATEX(\d+)%%\s*<\/p>/g, (_, index) => renderTex(store[Number(index)]))
    .replace(/%%KATEX(\d+)%%/g, (_, index) => renderTex(store[Number(index)]))
}
