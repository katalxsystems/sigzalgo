import { useEffect } from 'react'
import { bottomNavItems, navItems } from '@/config/navigation'
import { useMenuVisibilityStore } from '@/stores/menuVisibilityStore'

/**
 * Main navigation items (desktop navbar, mobile bottom nav, mobile sheet),
 * filtered by admin-configured per-role visibility -- see
 * blueprints/menu_visibility.py and useProfileMenuItems (the same pattern,
 * applied to config/navigation.ts's navItems instead of profileMenuItems).
 *
 * mobileSheetItems is derived here (not imported from config/navigation)
 * because it must be computed from the *filtered* navItems/bottomNavItems,
 * not the static unfiltered arrays -- otherwise a hidden top-level item
 * would still leak into the mobile "more" sheet.
 */
export function useNavItems() {
  const { hiddenItems, isLoaded, fetchHiddenItems } = useMenuVisibilityStore()

  useEffect(() => {
    if (!isLoaded) {
      fetchHiddenItems()
    }
  }, [isLoaded, fetchHiddenItems])

  const filteredNavItems = navItems.filter((item) => !hiddenItems.includes(item.href))
  const filteredBottomNavItems = bottomNavItems.filter((item) => !hiddenItems.includes(item.href))
  const bottomNavPaths = filteredBottomNavItems.map((item) => item.href)
  const filteredMobileSheetItems = filteredNavItems.filter(
    (item) => !bottomNavPaths.includes(item.href)
  )

  return {
    navItems: filteredNavItems,
    bottomNavItems: filteredBottomNavItems,
    mobileSheetItems: filteredMobileSheetItems,
  }
}
