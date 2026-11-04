// API client: workspace token handling, error normalisation, SSE chat streaming.
import axios from 'axios'

export const API = import.meta.env.VITE_API_URL || ''
const KEY = 'talkdox.token'

const read = () => { try { return localStorage.getItem(KEY) } catch { return null } }
const write = (v) => { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY) } catch { /* private mode */ } }

let token = read()
let creating = null

export const http = axios.create({ baseURL: API, timeout: 180000 })

export function ensureWorkspace() {
  if (token) return Promise.resolve(token)
  if (!creating) {
    creating = axios.post(`${API}/api/workspaces`).then(r => {
      token = r.data.token
      write(token)
      return token
    }).finally(() => { creating = null })
  }
  return creating
}

http.interceptors.request.use(cfg => {
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

// An expired/unknown token (server restarted with a fresh DB, retention purge...) -> start a new workspace
// transparently and tell the UI so it can explain why earlier sources are gone.
http.interceptors.response.use(r => r, async err => {
  const cfg = err.config
  if (err.response?.data?.code === 'invalid_token' && cfg && !cfg._retried) {
    token = null
    write(null)
    window.dispatchEvent(new CustomEvent('talkdox:session-reset'))
    await ensureWorkspace()
    cfg._retried = true
    cfg.headers.Authorization = `Bearer ${token}`
    return http(cfg)
  }
  return Promise.reject(err)
})

export function errorMessage(e, fallback = 'Something went wrong. Please try again.') {
  if (e?.name === 'CanceledError' || e?.name === 'AbortError') return 'Cancelled.'
  const d = e?.response?.data
  if (d?.error) return d.error
  if (e?.response?.status === 413) return 'That upload is too large.'
  if (e?.code === 'ERR_NETWORK' || e?.message === 'Network Error') return "Can't reach the TalkDox server. Check your connection and try again."
  if (e?.code === 'ECONNABORTED') return 'The request took too long. Please try again.'
  return e?.message && !/status code/.test(e.message) ? e.message : fallback
}

export function errorCode(e) { return e?.response?.data?.code || null }

/** Stream a chat answer over SSE (POST + fetch, so we can send the bearer token). */
export async function streamChat(body, { onStage, signal }) {
  await ensureWorkspace()
  const res = await fetch(`${API}/api/chat/stream`, {
    method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  })
  if (!res.ok || !res.body) {
    let d = {}
    try { d = await res.json() } catch { /* ignore */ }
    const err = new Error(d.error || `Request failed (${res.status})`)
    err.response = { data: d, status: res.status }
    throw err
  }
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = '', final = null
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2)
      const ev = /^event: (.*)$/m.exec(block)?.[1]
      const data = /^data: (.*)$/m.exec(block)?.[1]
      if (!ev || !data) continue
      const payload = JSON.parse(data)
      if (ev === 'stage') onStage?.(payload)
      else if (ev === 'final') final = payload
      else if (ev === 'error') {
        const err = new Error(payload.error)
        err.response = { data: payload }
        throw err
      }
    }
  }
  if (!final) throw new Error('The answer stream ended unexpectedly.')
  return final
}
