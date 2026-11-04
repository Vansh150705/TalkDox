import { useWorkspace } from '../context/ctx'

export default function ConnectionGate({ children }) {
  const ws = useWorkspace()
  if (ws.state === 'ready') {
    return (<>
      {ws.notice && <div className="banner" role="status">{ws.notice} <button type="button" className="link-btn" onClick={() => ws.setNotice('')}>Dismiss</button></div>}
      {ws.config?.demo && <div className="banner warn" role="status">Offline demo mode: no AI model is connected. Answers are sentences extracted from your sources by a simple test reader, not written by an AI.</div>}
      {ws.config?.problems?.length > 0 && <div className="banner warn" role="alert">{ws.config.problems.join(' ')}</div>}
      {children}
    </>)
  }
  return (
    <div className="gate" role="status" aria-live="polite">
      <div className="logo big">TalkDox 🧠</div>
      {ws.state === 'connecting' ? (
        <>
          <h2>Connecting to the server…</h2>
          <p className="muted">Free servers go to sleep when idle, so the first connection can take up to a minute.</p>
          <div className="dots"><i /><i /><i /></div>
        </>
      ) : (
        <>
          <h2>Can’t reach the TalkDox server</h2>
          <p className="muted">Check your internet connection, or try again in a moment.</p>
          <button type="button" className="btn primary" onClick={() => ws.connect(true)}>Try again</button>
        </>
      )}
    </div>
  )
}
