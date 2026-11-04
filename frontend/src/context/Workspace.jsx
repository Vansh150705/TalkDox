import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Ctx } from './ctx'
import { ensureWorkspace, errorMessage, http } from '../api'
import { isBusy, isReady } from '../format'

export function WorkspaceProvider({ children }) {
  const [state, setState] = useState('connecting') // connecting | ready | offline
  const [config, setConfig] = useState(null)
  const [sources, setSources] = useState([])
  const [selected, setSelected] = useState(() => new Set())
  const [notice, setNotice] = useState('')
  const knownIds = useRef(new Set())
  const deselected = useRef(new Set())

  const loadSources = useCallback(async () => {
    const r = await http.get('/api/sources')
    const list = r.data.sources
    setSources(list)
    // newly ready sources are selected by default; user choices are respected afterwards
    setSelected(prev => {
      const next = new Set([...prev].filter(id => list.some(s => s.id === id)))
      for (const s of list) {
        if (isReady(s) && !knownIds.current.has(s.id) && !deselected.current.has(s.id)) next.add(s.id)
        if (isReady(s)) knownIds.current.add(s.id)
      }
      return next
    })
    return list
  }, [])

  const connect = useCallback(async (manual = false) => {
    if (manual) setState('connecting')
    // Free hosts sleep when idle; retry for up to ~60s before showing the offline state.
    for (let attempt = 0; attempt < 12; attempt++) {
      try {
        await ensureWorkspace()
        const [c] = await Promise.all([http.get('/api/config'), loadSources()])
        setConfig(c.data)
        setState('ready')
        return
      } catch (e) {
        if (e?.response?.status && e.response.status < 500 && e.response.data?.code !== 'invalid_token') break
        await new Promise(r => setTimeout(r, 5000))
      }
    }
    setState('offline')
  }, [loadSources])

  useEffect(() => {
    const t = setTimeout(() => connect(false), 0)
    return () => clearTimeout(t)
  }, [connect])

  useEffect(() => {
    const onReset = () => {
      knownIds.current = new Set(); deselected.current = new Set(); setSelected(new Set()); setSources([])
      setNotice('Your previous workspace is no longer available on the server (it may have been deleted after inactivity), so a new empty workspace was started.')
    }
    window.addEventListener('talkdox:session-reset', onReset)
    return () => window.removeEventListener('talkdox:session-reset', onReset)
  }, [])

  // poll while anything is processing
  const busy = sources.some(isBusy)
  useEffect(() => {
    if (!busy || state !== 'ready') return
    const t = setInterval(() => { loadSources().catch(() => {}) }, 1200)
    return () => clearInterval(t)
  }, [busy, state, loadSources])

  const toggle = useCallback((id) => {
    setSelected(prev => {
      const n = new Set(prev)
      if (n.has(id)) { n.delete(id); deselected.current.add(id) } else { n.add(id); deselected.current.delete(id) }
      return n
    })
  }, [])

  const selectOnly = useCallback((ids) => {
    deselected.current = new Set(sources.map(s => s.id).filter(id => !ids.includes(id)))
    setSelected(new Set(ids))
  }, [sources])

  const addFiles = useCallback(async (files, onProgress) => {
    const fd = new FormData()
    files.forEach(f => fd.append('files', f))
    const r = await http.post('/api/sources/files', fd, { onUploadProgress: e => onProgress?.(e.total ? e.loaded / e.total : 0) })
    await loadSources()
    return r.data.results
  }, [loadSources])

  const addWeb = useCallback(async (body) => {
    const r = await http.post('/api/sources/web', body)
    await loadSources()
    return r.data.results
  }, [loadSources])

  const addYouTube = useCallback(async (url, langs = []) => {
    const r = await http.post('/api/sources/youtube', { url, langs })
    await loadSources()
    return r.data.results
  }, [loadSources])

  const removeSource = useCallback(async (id) => {
    await http.delete(`/api/sources/${id}`)
    knownIds.current.delete(id)
    await loadSources()
  }, [loadSources])

  const refreshSource = useCallback(async (id) => {
    await http.post(`/api/sources/${id}/refresh`)
    await loadSources()
  }, [loadSources])

  const cancelJob = useCallback(async (jobId) => {
    await http.post(`/api/jobs/${jobId}/cancel`)
  }, [])

  const deleteEverything = useCallback(async () => {
    await http.delete('/api/workspace')
    knownIds.current = new Set(); deselected.current = new Set()
    setSources([]); setSelected(new Set())
    await ensureWorkspace()
    await loadSources()
  }, [loadSources])

  const value = useMemo(() => ({
    state, config, sources, selected, notice, setNotice, connect, loadSources, toggle, selectOnly, addFiles, addWeb,
    addYouTube, removeSource, refreshSource, cancelJob, deleteEverything, errorMessage,
    readySources: sources.filter(isReady), selectedReady: sources.filter(s => isReady(s) && selected.has(s.id)),
  }), [state, config, sources, selected, notice, connect, loadSources, toggle, selectOnly, addFiles, addWeb, addYouTube,
    removeSource, refreshSource, cancelJob, deleteEverything])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
