import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'react-hot-toast'
import { LayoutProvider } from './context/LayoutContext'
import { AuthProvider } from './context/AuthContext'
import App from './App'
import './index.css'

const savedTheme = localStorage.getItem('rt-theme-mode') || 'dark'
document.documentElement.setAttribute('data-theme', savedTheme)

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30000,
      retry: 1,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <LayoutProvider>
            <App />
            <Toaster
              position="bottom-right"
              toastOptions={{
                style: {
                  background: 'var(--rt-toast-bg)',
                  color: 'var(--rt-toast-text)',
                  border: '1px solid var(--rt-toast-border)',
                },
              }}
            />
          </LayoutProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
)
