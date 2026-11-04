import { Outlet, Route, Routes } from 'react-router-dom'
import Landing from './pages/Landing'
import Upload from './pages/Upload'
import Chat from './pages/Chat'
import { WorkspaceProvider } from './context/Workspace'

// The workspace (and its server connection) only exists on the app pages - visiting the landing page never
// creates a workspace or wakes the backend.
function WorkspaceLayout() {
  return <WorkspaceProvider><Outlet /></WorkspaceProvider>
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route element={<WorkspaceLayout />}>
        <Route path="/upload" element={<Upload />} />
        <Route path="/chat" element={<Chat />} />
      </Route>
      <Route path="*" element={<Landing />} />
    </Routes>
  )
}
