import { Compass } from 'lucide-react'
import { Link, Outlet, Route, Routes } from 'react-router'
import { Header } from './components/Header'
import { EmptyState } from './components/ui'
import { ChatPage } from './pages/ChatPage'
import { DocumentDetailPage } from './pages/DocumentDetailPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { MetricsPage } from './pages/MetricsPage'

/**
 * Client-side routes. FastAPI serves index.html for any unknown path (see app/main.py), so deep links such as
 * /chat/<id> work on refresh in production as well as under the Vite dev server.
 */
export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DocumentsPage />} />
        <Route path="documents/:documentId" element={<DocumentDetailPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="chat/:sessionId" element={<ChatPage />} />
        <Route path="metrics" element={<MetricsPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}

function Layout() {
  return (
    <div className="min-h-full">
      <Header />
      <Outlet />
    </div>
  )
}

function NotFound() {
  return (
    <EmptyState icon={Compass} title="Page not found" action={<Link to="/" className="text-sm font-medium text-accent-600 hover:text-accent-700">Go to documents</Link>}>
      This address does not match any page in the dashboard.
    </EmptyState>
  )
}
