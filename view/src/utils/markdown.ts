import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'

// 显式标注类型：highlight 内部要用 md.utils.escapeHtml，不标注会形成
// "md 的类型依赖 highlight 的返回类型、highlight 又依赖 md" 的循环推断（TS7022）。
const md: MarkdownIt = new MarkdownIt({
  html: false, // 防 XSS：用户内容无法注入原始 HTML
  linkify: true,
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        const out = hljs.highlight(str, { language: lang, ignoreIllegals: true }).value
        return `<pre class="hljs"><code>${out}</code></pre>`
      } catch {
        /* fall through */
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})

export function renderMarkdown(text: string): string {
  return md.render(text ?? '')
}
