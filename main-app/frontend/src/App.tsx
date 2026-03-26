import { Routes, Route, Navigate } from 'react-router-dom'
import { useLayout } from './context/LayoutContext'
import { useAuth } from './context/AuthContext'
import Layout from './components/Layout'
import LayoutCommandCenter from './components/LayoutCommandCenter'
import Dashboard from './pages/Dashboard'
import Pods from './pages/Pods'
import PodDetail from './pages/PodDetail'
import Products from './pages/Products'
import Agent from './pages/Agent'
import SOPs from './pages/SOPs'
import Documentation from './pages/Documentation'
import Settings from './pages/Settings'
import Login from './pages/Login'
import LoginPassword from './pages/LoginPassword'
import AzureADCallback from './pages/AzureADCallback'
import FirstLoginSetup from './pages/FirstLoginSetup'
import ResetPassword from './pages/ResetPassword'
import SetupPage from './pages/SetupPage'
import UserManagement from './pages/UserManagement'
import McpBuilder from './pages/McpBuilder'
import { Loader2 } from 'lucide-react'

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, user } = useAuth()
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-rt-bg">
        <Loader2 className="w-8 h-8 animate-spin text-rt-primary" />
      </div>
    )
  }
  if (!isAuthenticated) return <Navigate to="/login" replace />
  if (user?.force_password_change) return <Navigate to="/setup" replace />
  return <>{children}</>
}

function App() {
  const { layoutMode } = useLayout()
  const LayoutComponent = layoutMode === 'command-center' ? LayoutCommandCenter : Layout

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/login/password" element={<LoginPassword />} />
      <Route path="/auth/callback/azure" element={<AzureADCallback />} />
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/reset-password" element={<ResetPassword />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <LayoutComponent />
          </ProtectedRoute>
        }
      >
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<Dashboard />} />
        <Route path="pods" element={<Pods />} />
        <Route path="pods/:podId" element={<PodDetail />} />
        <Route path="groups" element={<Navigate to="/products" replace />} />
        <Route path="products" element={<Products />} />
        <Route path="agent" element={<Agent />} />
        <Route path="sops" element={<SOPs />} />
        <Route path="docs" element={<Documentation />} />
        <Route path="settings" element={<Settings />} />
        <Route path="users" element={<UserManagement />} />
        <Route path="mcp-builder" element={<McpBuilder />} />
      </Route>
    </Routes>
  )
}

export default App
