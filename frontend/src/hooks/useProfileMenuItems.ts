import { useEffect } from 'react'
import { profileMenuItems } from '@/config/navigation'
import { useBrokerStore } from '@/stores/brokerStore'
import { useMenuVisibilityStore } from '@/stores/menuVisibilityStore'

/**
 * Profile dropdown items filtered by broker capabilities and admin-configured
 * per-role visibility.
 *
 * Single source of truth for menu gating, used by the shared Navbar AND the
 * standalone full-screen layouts (Playground, Historify, Historify Charts,
 * Flow Editor) so crypto-only items (Leverage) and equity-only items
 * (Holdings) never leak into the wrong broker's menus (GitHub issue #1480),
 * and so an admin-hidden item (blueprints/menu_visibility.py) disappears
 * everywhere the menu is rendered, not just the main Navbar.
 */
export function useProfileMenuItems() {
  const { capabilities, isLoaded, fetchCapabilities } = useBrokerStore()
  const { hiddenItems, isLoaded: hiddenItemsLoaded, fetchHiddenItems } = useMenuVisibilityStore()

  // Standalone layouts can mount without the main app shell having fetched
  // capabilities yet — fetch on demand so gating never runs on stale null.
  useEffect(() => {
    if (!isLoaded) {
      fetchCapabilities()
    }
  }, [isLoaded, fetchCapabilities])

  useEffect(() => {
    if (!hiddenItemsLoaded) {
      fetchHiddenItems()
    }
  }, [hiddenItemsLoaded, fetchHiddenItems])

  return profileMenuItems.filter((item) => {
    if (hiddenItems.includes(item.href)) return false
    if (item.href === '/leverage') return capabilities?.leverage_config === true
    if (item.href === '/holdings') return capabilities?.broker_type !== 'crypto'
    return true
  })
}
