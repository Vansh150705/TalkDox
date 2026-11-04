import { useState } from 'react'

const KEY = 'talkdox.onboarded'
const STEPS = [
  { icon: '📥', title: 'Add what you want to understand', body: 'Upload PDFs, Word, PowerPoint, Excel or CSV files, paste web page links (even whole documentation sites), or add a YouTube video.' },
  { icon: '🔎', title: 'Ask questions in plain language', body: 'TalkDox searches your sources and writes an answer only from what it finds, with numbered citations.' },
  { icon: '✅', title: 'Check the evidence', body: 'Click any citation to see the exact passage — the page, the web section or the video moment. If something isn’t in your sources, TalkDox says so instead of guessing.' },
]

export default function Onboarding({ force, onClose }) {
  const [step, setStep] = useState(0)
  const [seen, setSeen] = useState(() => { try { return !!localStorage.getItem(KEY) } catch { return true } })
  if (seen && !force) return null
  const done = () => { try { localStorage.setItem(KEY, '1') } catch { /* ignore */ } setSeen(true); setStep(0); onClose?.() }
  const s = STEPS[step]
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Welcome to TalkDox">
      <div className="modal small">
        <div className="onb-icon" aria-hidden="true">{s.icon}</div>
        <h2>{s.title}</h2>
        <p>{s.body}</p>
        <div className="dots-nav" aria-hidden="true">{STEPS.map((_, i) => <i key={i} className={i === step ? 'on' : ''} />)}</div>
        <div className="row end">
          <button type="button" className="link-btn" onClick={done}>Skip</button>
          {step < STEPS.length - 1 ? <button type="button" className="btn primary" onClick={() => setStep(step + 1)}>Next</button> : <button type="button" className="btn primary" onClick={done}>Get started</button>}
        </div>
      </div>
    </div>
  )
}
