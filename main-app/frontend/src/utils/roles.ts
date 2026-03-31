/**
 * Centralized role utilities for the 3-level enterprise role system.
 *
 * Roles (descending privilege):
 *   zero_admin  (110) — Retrace platform super admin (one per deployment)
 *   admin       (100) — Tenant admin, full access
 *   user_admin  (60)  — LOB admin, can add/manage their own users only
 *   user        (20)  — Regular user, no management capability
 */

export const ROLE_HIERARCHY: Record<string, number> = {
  zero_admin: 110,
  admin: 100,
  super_admin: 100,   // legacy alias
  tenant_admin: 100,  // legacy alias
  user_admin: 60,
  user: 20,
}

/** Returns true if userRole meets or exceeds the required role level. */
export const hasRole = (userRole: string | undefined, required: string): boolean =>
  (ROLE_HIERARCHY[userRole ?? ''] ?? 0) >= (ROLE_HIERARCHY[required] ?? 0)

/** True for admin and zero_admin (full platform access). */
export const isAdmin = (role?: string): boolean => hasRole(role, 'admin')

/** True for user_admin only (not admin or above). */
export const isUserAdmin = (role?: string): boolean =>
  hasRole(role, 'user_admin') && !isAdmin(role)

/** True for plain user only. */
export const isRegularUser = (role?: string): boolean => role === 'user'

/**
 * Returns the list of role values an actor is allowed to assign.
 * Matches backend can_provision_role() in security.py.
 */
export const assignableRoles = (actorRole: string | undefined): string[] => {
  if (isAdmin(actorRole)) return ['admin', 'user_admin', 'user']
  if (actorRole === 'user_admin') return ['user']
  return []
}

/** Human-readable label for a role value. */
export const roleLabel = (role: string): string => {
  const labels: Record<string, string> = {
    zero_admin: 'Platform Admin',
    admin: 'Admin',
    super_admin: 'Admin',
    tenant_admin: 'Admin',
    user_admin: 'User Admin',
    user: 'User',
  }
  return labels[role] ?? role
}
