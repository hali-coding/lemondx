import { createContext, useContext } from 'react'
import type { AuthInfo, Role } from '../lib/types'

/** What /api/auth last said; null until it has answered. */
export const AuthContext = createContext<AuthInfo | null>(null)

export function useAuthInfo() {
  return useContext(AuthContext)
}

// Mirrors auth._RANK: each role can do everything the ones below it can.
const RANK: Record<Role, number> = { read: 1, operator: 2, admin: 3 }

export function roleAtLeast(held: Role, wanted: Role) {
  return (RANK[held] ?? 0) >= RANK[wanted]
}

/** Whether a principal holds at least `role`. No principal means auth is off: everything. */
export function hasRole(principal: AuthInfo['principal'] | undefined, role: Role) {
  return !principal || roleAtLeast(principal.role, role)
}

/**
 * Whether to offer controls that change things. Only a convenience: the
 * server refuses a caller's changes whatever the page shows, so while the
 * answer is unknown, controls stay enabled rather than flicker.
 */
export function useCanWrite() {
  return hasRole(useContext(AuthContext)?.principal, 'admin')
}

/**
 * Whether to offer controls that run what exists -- start, stop, destroy,
 * launch a saved template or stack, run a command -- without editing it.
 * Server routes marked `role=OPERATOR` are the other half of this.
 */
export function useCanOperate() {
  return hasRole(useContext(AuthContext)?.principal, 'operator')
}
