import { create } from 'zustand'

interface MenuVisibilityStore {
  hiddenItems: string[]
  isLoaded: boolean

  fetchHiddenItems: () => Promise<void>
  clearHiddenItems: () => void
}

/**
 * Admin-configured list of profile-menu item keys (href values) hidden for
 * the current session's role — see blueprints/menu_visibility.py. Shared
 * store so the Navbar and any standalone layout using useProfileMenuItems
 * don't each fire their own fetch.
 */
export const useMenuVisibilityStore = create<MenuVisibilityStore>()((set) => ({
  hiddenItems: [],
  isLoaded: false,

  fetchHiddenItems: async () => {
    try {
      const response = await fetch('/api/menu-visibility', {
        credentials: 'include',
      })

      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          set({ hiddenItems: data.hidden_items || [], isLoaded: true })
        }
      }
    } catch {
      // Silently fail — hiddenItems stays empty, nothing gets hidden
    }
  },

  clearHiddenItems: () => set({ hiddenItems: [], isLoaded: false }),
}))
