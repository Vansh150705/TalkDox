// Minimal, XSS-safe markdown renderer: builds React elements (never innerHTML). Supports headings, lists,
// tables, quotes, code, bold/italic/inline code, http(s) links and [S1]-style citation markers.
import { Fragment } from 'react'

const CITE = /\[S(\d+)\]/g

function inline(text, onCite, key = '') {
  const out = []
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))|(\[S\d+\])/g
  let last = 0, m, i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const k = `${key}-${i++}`
    if (m[1]) out.push(<code key={k}>{m[1].slice(1, -1)}</code>)
    else if (m[2]) out.push(<strong key={k}>{inline(m[2].slice(2, -2), onCite, k)}</strong>)
    else if (m[3]) out.push(<em key={k}>{inline(m[3].slice(1, -1), onCite, k)}</em>)
    else if (m[4]) {
      const lm = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/.exec(m[4])
      out.push(<a key={k} href={lm[2]} target="_blank" rel="noopener noreferrer nofollow">{lm[1]}</a>)
    } else if (m[6]) {
      const n = Number(m[6].slice(2, -1))
      out.push(
        <button key={k} type="button" className="cite-chip" onClick={() => onCite?.(n)} aria-label={`Open source ${n}`}>{n}</button>
      )
    }
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function splitRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(c => c.replace(/\\\|/g, '|').trim())
}

export default function Markdown({ text, onCite }) {
  const lines = (text || '').replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }
    if (/^```/.test(line)) {
      const code = []; i++
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++])
      i++
      blocks.push(<pre key={blocks.length}><code>{code.join('\n')}</code></pre>)
      continue
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) { const L = Math.min(h[1].length + 2, 6); const Tag = `h${L}`; blocks.push(<Tag key={blocks.length}>{inline(h[2], onCite, `h${i}`)}</Tag>); i++; continue }
    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const head = splitRow(line); i += 2
      const rows = []
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(splitRow(lines[i++]))
      blocks.push(
        <div className="md-table" key={blocks.length}><table>
          <thead><tr>{head.map((c, j) => <th key={j}>{inline(c, onCite, `th${j}`)}</th>)}</tr></thead>
          <tbody>{rows.map((r, a) => <tr key={a}>{r.map((c, b) => <td key={b}>{inline(c, onCite, `td${a}${b}`)}</td>)}</tr>)}</tbody>
        </table></div>
      )
      continue
    }
    if (/^\s*([-*+])\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line)
      const items = []
      while (i < lines.length && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]))) items.push(lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, ''))
      const Tag = ordered ? 'ol' : 'ul'
      blocks.push(<Tag key={blocks.length}>{items.map((it, j) => <li key={j}>{inline(it, onCite, `li${i}${j}`)}</li>)}</Tag>)
      continue
    }
    if (/^>\s?/.test(line)) {
      const q = []
      while (i < lines.length && /^>\s?/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/, ''))
      blocks.push(<blockquote key={blocks.length}>{inline(q.join(' '), onCite, `q${i}`)}</blockquote>)
      continue
    }
    if (/^---+$/.test(line.trim())) { blocks.push(<hr key={blocks.length} />); i++; continue }
    const para = []
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|>\s?|\s*([-*+]|\d+[.)])\s+|\s*\|)/.test(lines[i])) para.push(lines[i++])
    if (!para.length) { para.push(lines[i++]) }
    blocks.push(<p key={blocks.length}>{para.map((p, j) => <Fragment key={j}>{j > 0 && <br />}{inline(p, onCite, `p${i}${j}`)}</Fragment>)}</p>)
  }
  return <div className="md">{blocks}</div>
}

export { CITE }
