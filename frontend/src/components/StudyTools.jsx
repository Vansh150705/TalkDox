import { useMemo, useState } from 'react'
import { useWorkspace } from '../context/ctx'
import { errorMessage, http } from '../api'
import Markdown from './Markdown'
import { CitationCard, ConfidenceBadge, Flags } from './Citations'
import { plural } from '../format'

const TOOLS = [
  { id: 'summary', icon: '📝', name: 'Summary', desc: 'Structured summary of the whole source (not just the start).' },
  { id: 'dna', icon: '🧬', name: 'Source profile', desc: 'Domain, tone, key themes and entities, reading time.' },
  { id: 'flashcards', icon: '🃏', name: 'Flashcards', desc: 'Study cards covering the material, each tied to a passage.' },
  { id: 'quiz', icon: '❓', name: 'Quiz', desc: 'Multiple-choice questions with explanations and citations.' },
  { id: 'timeline', icon: '🕐', name: 'Timeline', desc: 'Dates, deadlines and milestones that are explicitly stated.' },
  { id: 'actions', icon: '✅', name: 'Action items', desc: 'Tasks, owners and deadlines found in the text.' },
  { id: 'contradictions', icon: '⚡', name: 'Contradictions', desc: 'Statements that conflict with each other.' },
  { id: 'email', icon: '✉️', name: 'Email draft', desc: 'A cited email based on the source.' },
  { id: 'compare', icon: '🔀', name: 'Compare sources', desc: 'Similarities and differences across 2+ selected sources.' },
]

function Cites({ citations, onOpen }) {
  const [open, setOpen] = useState(false)
  if (!citations?.length) return null
  return (
    <div className="tool-cites">
      <button type="button" className="link-btn" onClick={() => setOpen(o => !o)} aria-expanded={open}>{plural(citations.length, 'supporting passage')} {open ? '▲' : '▼'}</button>
      {open && citations.map((c, i) => <CitationCard key={c.id} c={c} index={c.id.replace(/^\D+/, '') || i + 1} onOpen={onOpen} />)}
    </div>
  )
}

const CiteTag = ({ id, onJump }) => id ? <button type="button" className="cite-chip" onClick={() => onJump(id)} aria-label={`Show source ${id}`}>{String(id).replace(/^\D+/, '')}</button> : null

function Flashcards({ items, jump }) {
  const [level, setLevel] = useState('all')
  const [flipped, setFlipped] = useState({})
  const shown = items.filter(c => level === 'all' || c.difficulty === level)
  return (
    <>
      <div className="seg" role="group" aria-label="Difficulty filter">
        {['all', 'easy', 'medium', 'hard'].map(l => <button key={l} type="button" aria-pressed={level === l} className={level === l ? 'on' : ''} onClick={() => setLevel(l)}>{l}</button>)}
      </div>
      <div className="cards">
        {shown.map((c, i) => (
          <button type="button" key={i} className={`flash ${flipped[i] ? 'flipped' : ''} d-${c.difficulty}`} onClick={() => setFlipped(f => ({ ...f, [i]: !f[i] }))}
                  aria-label={flipped[i] ? `Answer: ${c.answer}. Press to see the question.` : `Question: ${c.question}. Press to reveal the answer.`}>
            <span className="flash-inner">
              <span className="flash-face front"><small>{c.difficulty} · {c.topic}</small><b>{c.question}</b><em>Tap to reveal</em></span>
              <span className="flash-face back"><small>Answer</small><b>{c.answer}</b><span onClick={e => e.stopPropagation()}><CiteTag id={c.cite} onJump={jump} /></span></span>
            </span>
          </button>
        ))}
      </div>
    </>
  )
}

function Quiz({ items, jump }) {
  const [picked, setPicked] = useState({})
  const score = Object.entries(picked).filter(([i, p]) => items[i].correct_index === p).length
  return (
    <>
      <div className="muted">Answered {Object.keys(picked).length}/{items.length} · Score {score}</div>
      {items.map((q, i) => (
        <fieldset key={i} className="quiz-q">
          <legend>{i + 1}. {q.question}</legend>
          {q.options.map((o, j) => {
            const done = picked[i] != null
            const cls = done ? (j === q.correct_index ? 'right' : j === picked[i] ? 'wrong' : '') : ''
            return <label key={j} className={`opt ${cls}`}><input type="radio" name={`q${i}`} disabled={done} checked={picked[i] === j} onChange={() => setPicked(p => ({ ...p, [i]: j }))} /> {o}</label>
          })}
          {picked[i] != null && <div className="explain">{q.explanation} <CiteTag id={q.cite} onJump={jump} /></div>}
        </fieldset>
      ))}
    </>
  )
}

const TL_COLORS = { deadline: '#dc2626', milestone: '#16a34a', event: '#2563eb', period: '#d97706', announcement: '#9333ea', other: '#6b7280' }

export default function StudyTools({ onOpen }) {
  const ws = useWorkspace()
  const [tool, setTool] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [count, setCount] = useState(10)
  const [goal, setGoal] = useState('')
  const [question, setQuestion] = useState('')
  const ids = useMemo(() => ws.selectedReady.map(s => s.id), [ws.selectedReady])

  const pick = (id) => { setTool(id); setResult(null); setError('') }

  const run = async (refresh = false) => {
    setBusy(true); setError(''); setResult(null)
    try {
      const r = tool === 'compare'
        ? await http.post('/api/compare', { source_ids: ids, question: question.trim() || undefined })
        : await http.post(`/api/tools/${tool}`, { source_ids: tool === 'dna' ? ids.slice(0, 1) : ids, options: { count, goal, refresh } })
      setResult(r.data)
    } catch (e) { setError(errorMessage(e)) } finally { setBusy(false) }
  }

  const jump = (id) => {
    const c = result?.citations?.find(x => x.id === id)
    if (c) onOpen(c)
  }
  const info = TOOLS.find(t => t.id === tool)
  const needsTwo = tool === 'compare' && ids.length < 2
  const disabled = ids.length === 0 || needsTwo || busy

  return (
    <div className="tools">
      {!tool && (
        <>
          <p className="muted">Every result is generated from your selected sources and carries citations. {ids.length === 0 && 'Select a source first.'}</p>
          <div className="toolgrid">
            {TOOLS.map(t => (
              <button key={t.id} type="button" className="toolcard" onClick={() => pick(t.id)}>
                <span className="toolicon" aria-hidden="true">{t.icon}</span><b>{t.name}</b><span>{t.desc}</span>
              </button>
            ))}
          </div>
        </>
      )}
      {tool && (
        <>
          <button type="button" className="link-btn" onClick={() => pick(null)}>← All tools</button>
          <h2>{info.icon} {info.name}</h2>
          <p className="muted">{info.desc} Using {plural(ids.length, 'selected source')}.</p>
          <div className="toolbar">
            {(tool === 'quiz' || tool === 'flashcards') && <label>How many <input className="input mini" type="number" min={3} max={30} value={count} onChange={e => setCount(+e.target.value)} /></label>}
            {tool === 'email' && <input className="input grow" placeholder="Purpose of the email (optional)" value={goal} onChange={e => setGoal(e.target.value)} />}
            {tool === 'compare' && <input className="input grow" placeholder="Optional: a specific comparison question" value={question} onChange={e => setQuestion(e.target.value)} />}
            <button type="button" className="btn primary" disabled={disabled} onClick={() => run(false)}>{busy ? 'Working…' : result ? 'Regenerate' : 'Generate'}</button>
          </div>
          {needsTwo && <div className="alert alert-info">Select at least two sources in the Sources panel.</div>}
          {error && <div className="alert alert-error">{error}</div>}
          {busy && <div className="skeleton tall" aria-label="Generating" />}
          {result && !busy && (
            <div className="result">
              {result.cached && <div className="hint">Showing the saved result. <button type="button" className="link-btn" onClick={() => run(true)}>Regenerate</button></div>}
              {result.note && <div className="alert alert-info">{result.note}</div>}
              {result.coverage && !result.coverage.complete && <div className="hint">Analysed {result.coverage.chunks_used} of {result.coverage.chunks_total} passages.</div>}
              {result.dropped_unsupported > 0 && <div className="hint">{plural(result.dropped_unsupported, 'generated item')} had no verifiable source passage and {result.dropped_unsupported > 1 ? 'were' : 'was'} discarded.</div>}

              {(tool === 'summary' || tool === 'email' || tool === 'compare') && (
                <>
                  <ConfidenceBadge conf={result.confidence} />
                  <Markdown text={result.answer} onCite={n => jump(`S${n}`)} />
                  <Flags flags={result.flags} />
                  <Cites citations={result.citations} onOpen={onOpen} />
                </>
              )}
              {tool === 'dna' && (
                <div className="profile">
                  <div className="kv"><span>Title</span><b>{result.profile.title || '—'}</b></div>
                  <div className="kv"><span>Domain</span><b>{result.profile.domain}</b></div>
                  <div className="kv"><span>Tone</span><b>{result.profile.tone}</b></div>
                  <div className="kv"><span>Complexity</span><b>{result.profile.complexity}</b></div>
                  <div className="kv"><span>Audience</span><b>{result.profile.audience}</b></div>
                  <div className="kv"><span>Language</span><b>{result.profile.language}</b></div>
                  <div className="kv"><span>Reading time</span><b>~{result.stats.reading_time_min} min · ~{result.stats.approx_words.toLocaleString()} words</b></div>
                  <div className="kv wide"><span>Summary</span><b>{result.profile.one_line_summary}</b></div>
                  <div className="kv wide"><span>Themes</span><div className="chips">{result.profile.key_themes.map(t => <span key={t} className="chip static">{t}</span>)}</div></div>
                  <div className="kv wide"><span>Entities</span><div className="chips">{result.profile.key_entities.map(t => <span key={t} className="chip static">{t}</span>)}</div></div>
                </div>
              )}
              {tool === 'flashcards' && <><Flashcards items={result.items} jump={jump} /><Cites citations={result.citations} onOpen={onOpen} /></>}
              {tool === 'quiz' && <><Quiz items={result.items} jump={jump} /><Cites citations={result.citations} onOpen={onOpen} /></>}
              {tool === 'timeline' && (
                <>
                  {result.items.length === 0 && <p className="muted">No explicitly dated events were found in the selected sources.</p>}
                  <ol className="timeline">{result.items.map((e, i) => (
                    <li key={i} style={{ '--c': TL_COLORS[e.type] || TL_COLORS.other }}>
                      <div className="tl-date">{e.date}</div>
                      <div className="tl-body"><span className="tl-type">{e.type}{e.importance === 'high' ? ' · important' : ''}</span> {e.event} <CiteTag id={e.cite} onJump={jump} /></div>
                    </li>))}</ol>
                  <Cites citations={result.citations} onOpen={onOpen} />
                </>
              )}
              {tool === 'actions' && (
                <>
                  {result.items.length === 0 && <p className="muted">No explicit action items were found.</p>}
                  <ul className="actions">{result.items.map((a, i) => (
                    <li key={i}><b>{a.action}</b> <span className={`pri ${a.priority}`}>{a.priority}</span>
                      <div className="muted">{a.owner ? `Owner: ${a.owner}` : 'Owner: not stated'} · {a.deadline ? `Due: ${a.deadline}` : 'Deadline: not stated'} <CiteTag id={a.cite} onJump={jump} /></div></li>))}</ul>
                  <Cites citations={result.citations} onOpen={onOpen} />
                </>
              )}
              {tool === 'contradictions' && (
                <>
                  {result.items.length === 0 && <p className="muted">No contradictions were found in the analysed passages.</p>}
                  {result.items.map((c, i) => (
                    <div key={i} className="conflict"><b>{c.summary}</b>
                      <div className="two"><div>“{c.claim_a}” <CiteTag id={c.cite_a} onJump={jump} /></div><div>“{c.claim_b}” <CiteTag id={c.cite_b} onJump={jump} /></div></div></div>))}
                  <Cites citations={result.citations} onOpen={onOpen} />
                </>
              )}
              <div className="hint">Generated by {result.model?.name || 'the AI model'}. Review before relying on it.</div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
