import { Navigate, Outlet } from 'react-router'
import { SocketProvider } from '@/components/socket/SocketProvider'
import { useAuthStore } from '@/stores/authStore'

/**
 * Full-width layout for apps like Playground that need maximum screen space.
 * No container constraints, minimal chrome.
 */
export function FullWidthLayout() {
  const { user } = useAuthStore()

  // `user` is non-null for any valid app session, whether or not a broker
  // is connected yet — see Layout.tsx for the same change and rationale.
  if (!user) {
    return <Navigate to="/login" replace />
  }

  return (
    <SocketProvider>
      <div className="h-screen bg-background flex flex-col overflow-hidden">
        <Outlet />
      </div>
    </SocketProvider>
  )
}
