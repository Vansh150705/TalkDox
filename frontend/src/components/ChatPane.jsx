import { useCallback, useEffect, useRef, useState } from 'react'
import { useWorkspace } from '../context/ctx'
import { errorCode, errorMessage, http, streamChat } from '../api'
import Markdown from './Markdown'
import { CitationCard, ConfidenceBadge, Flags, RelatedList } from './Citations'
import { plural } from '../format'

const MODES = [['qa', 'Precise answer'], ['eli5', 'Explain simply'], ['executive', 'Executive brief'], ['debate', 'Both sides']]
const PERSONAS = [['default', 'Neutral'], ['lawyer', 'Lawyer'], ['doctor', 'Clinician'], ['financial', 'Financial analyst'], ['teacher', 'Teacher'], ['journalist', 'Journalist']]

function Message({ m, onOpen, onFollow, onRetry }) {
  const [showSrc, setShowSrc] = useState(true)
  const [copied, setCopied] = useState(false)
  if (m.role === 'user') return <div className="msg user"><div className="bubble">{m.content}</div></div>
  const cites = m.citations || []
  const openByNumber = (n) => {
    const el = document.getElementById(`cite-${n}-${m.id}`)
    setShowSrc(true)
    setTimeout(() => (document.getElementById(`cite-${n}-${m.id}`) || el)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }), 50)
  }
  const copy = async () => {
    try { await navigator.clipboard.writeText(m.content.replace(/\[S\d+\]/g, '')); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* clipboard unavailable */ }
  }
  const kindClass = m.error ? 'error' : m.status === 'no_evidence' || m.status === 'unverified' ? 'noev' : ''
  return (
    <div className="msg ai">
      <div className="avatar" aria-hidden="true">🧠</div>
      <div className="ai-body">
        <div className={`bubble ${kindClass}`}>
          {m.error ? <div className="alert alert-error">{m.content}{m.retryable && <> <button type="button" className="link-btn" onClick={onRetry}>Try again</button></>}</div>
            : <Markdown text={m.content} onCite={openByNumber} />}
          {m.scope_note && <div className="scope">🎯 {m.scope_note}</div>}
          {m.note && <div className="scope">ℹ {m.note}</div>}
        </div>
        {!m.error && (
          <>
            <div className="msg-meta">
              {cites.length > 0 && <ConfidenceBadge conf={m.confidence} />}
              {m.status === 'no_evidence' && <span className="tag">Not found in sources</span>}
              {m.status === 'unverified' && <span className="tag warn">Couldn’t verify — nothing guessed</span>}
              {m.status === 'partial' && <span className="tag">Only partly answerable</span>}
              {cites.length > 0 && <button type="button" className="link-btn" onClick={() => setShowSrc(s => !s)} aria-expanded={showSrc}>{plural(cites.length, 'source')} {showSrc ? '▲' : '▼'}</button>}
              <button type="button" className="link-btn" onClick={copy}>{copied ? 'Copied ✓' : 'Copy'}</button>
              {m.model && <span className="model" title="Model that wrote this answer">{m.model.name}</span>}
            </div>
            <Flags flags={m.flags} />
            {showSrc && cites.length > 0 && (
              <div className="cites">{cites.map((c, i) => (
                <div id={`cite-${i + 1}-${m.id}`} key={c.id}><CitationCard c={c} index={i + 1} onOpen={onOpen} /></div>
              ))}</div>
            )}
            <RelatedList items={m.related} onOpen={onOpen} />
            {m.followups?.length > 0 && (
              <div className="followups">{m.followups.map(f => <button type="button" key={f} className="chip" onClick={() => onFollow(f)}>{f}</button>)}</div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

export default function ChatPane({ onOpen, conversationId, setConversationId, onConversationChange }) {
  const ws = useWorkspace()
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState('')
  const [mode, setMode] = useState('qa')
  const [persona, setPersona] = useState('default')
  const [listening, setListening] = useState(false)
  const abortRef = useRef(null)
  const endRef = useRef(null)
  const recRef = useRef(null)
  const lastQ = useRef('')
  const taRef = useRef(null)

  // load a stored conversation
  useEffect(() => {
    let cancelled = false
    if (!conversationId) { setMessages([]); return }
    http.get(`/api/conversations/${conversationId}`).then(r => {
      if (!cancelled) setMessages(r.data.messages.map(m => ({ ...m, id: m.id })))
    }).catch(() => { if (!cancelled) { setMessages([]); setConversationId(null) } })
    return () => { cancelled = true }
  }, [conversationId]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [messages, stage])

  const send = useCallback(async (text) => {
    const q = (text ?? input).trim()
    if (!q || busy) return
    lastQ.current = q
    setInput('')
    if (taRef.current) taRef.current.style.height = 'auto'
    setMessages(m => [...m, { role: 'user', content: q, id: `u${Date.now()}` }])
    setBusy(true); setStage('Starting…')
    const ac = new AbortController(); abortRef.current = ac
    try {
      const d = await streamChat({ question: q, source_ids: ws.selectedReady.map(s => s.id), conversation_id: conversationId, mode, persona },
        { onStage: p => setStage(p.message), signal: ac.signal })
      setMessages(m => [...m, { role: 'assistant', content: d.answer, id: d.message_id, ...d, content_: undefined }])
      if (!conversationId) { setConversationId(d.conversation_id) }
      onConversationChange?.()
    } catch (e) {
      if (e.name === 'AbortError') {
        setMessages(m => [...m, { role: 'assistant', error: true, content: 'Stopped.', id: `e${Date.now()}` }])
      } else {
        const code = errorCode(e)
        setMessages(m => [...m, { role: 'assistant', error: true, retryable: ['provider_rate_limited', 'provider_unavailable', 'provider_timeout', 'rate_limited', 'malformed_output', 'internal_error'].includes(code) || !code,
          content: errorMessage(e), id: `e${Date.now()}` }])
      }
    } finally { setBusy(false); setStage(''); abortRef.current = null }
  }, [input, busy, ws.selectedReady, conversationId, mode, persona, setConversationId, onConversationChange])

  const toggleVoice = () => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SR) { alert('Voice input needs a browser with speech recognition (Chrome, Edge or Safari).'); return }
    if (listening) { recRef.current?.stop(); return }
    const r = new SR()
    r.lang = navigator.language || 'en-US'; r.interimResults = false; r.continuous = false
    r.onstart = () => setListening(true)
    r.onresult = e => setInput(prev => (prev ? prev + ' ' : '') + e.results[e.results.length - 1][0].transcript)
    r.onerror = e => { setListening(false); if (e.error === 'not-allowed') alert('Microphone permission was denied.') }
    r.onend = () => setListening(false)
    recRef.current = r; r.start()
  }

  const ready = ws.selectedReady.length > 0
  const suggestions = ['Summarize this', 'What are the key points?', 'What dates or deadlines are mentioned?', 'List any numbers or figures that matter']
  const exportMd = () => window.open(`${import.meta.env.VITE_API_URL || ''}/api/conversations/${conversationId}/export`, '_blank')

  return (
    <div className="chat">
      <div className="chat-scroll" role="log" aria-live="polite" aria-label="Conversation">
        {messages.length === 0 && (
          <div className="empty">
            <div className="empty-icon" aria-hidden="true">💬</div>
            <h2>{ready ? 'Ask anything about your sources' : 'Select a source to begin'}</h2>
            <p className="muted">Answers are written only from your {ready ? plural(ws.selectedReady.length, 'selected source') : 'sources'}, with citations you can open. If the answer isn’t there, TalkDox says so.</p>
            {ready && <div className="followups center">{suggestions.map(s => <button key={s} type="button" className="chip" onClick={() => send(s)}>{s}</button>)}</div>}
          </div>
        )}
        {messages.map(m => <Message key={m.id} m={m} onOpen={onOpen} onFollow={send} onRetry={() => send(lastQ.current)} />)}
        {busy && <div className="msg ai"><div className="avatar" aria-hidden="true">🧠</div><div className="typing"><span className="dots"><i /><i /><i /></span> {stage}</div></div>}
        <div ref={endRef} />
      </div>

      <div className="composer">
        <div className="composer-opts">
          <label>Style <select className="input mini" value={mode} onChange={e => setMode(e.target.value)} aria-label="Answer style">{MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
          <label>Perspective <select className="input mini" value={persona} onChange={e => setPersona(e.target.value)} aria-label="Perspective">{PERSONAS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
          <span className="spacer" />
          {conversationId && <button type="button" className="link-btn" onClick={exportMd}>Export chat</button>}
          {messages.length > 0 && <button type="button" className="link-btn" onClick={() => { setConversationId(null); setMessages([]) }}>New chat</button>}
        </div>
        <div className="composer-row">
          <textarea ref={taRef} className="input grow" rows={1} value={input} placeholder={ready ? 'Ask a question about your sources…' : 'Add or select a source first'}
                    aria-label="Your question" disabled={!ready}
                    onChange={e => { setInput(e.target.value); e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px' }}
                    onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
          <button type="button" className={`icon-btn mic ${listening ? 'on' : ''}`} onClick={toggleVoice} aria-label={listening ? 'Stop voice input' : 'Start voice input'} aria-pressed={listening}>🎙</button>
          {busy
            ? <button type="button" className="btn" onClick={() => abortRef.current?.abort()}>Stop</button>
            : <button type="button" className="btn primary" disabled={!ready || !input.trim()} onClick={() => send()}>Send</button>}
        </div>
        <div className="composer-foot">AI can make mistakes — check the cited passages for anything important. Sent to {ws.config?.model?.provider || 'the AI provider'}: your question and the retrieved passages.</div>
      </div>
    </div>
  )
}
