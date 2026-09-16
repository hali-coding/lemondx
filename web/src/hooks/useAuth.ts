import { createContext, useContext } from 'react'
import type { AuthInfo } from '../lib/types'

/** What /api/auth last said; null until it has answered. */
export const AuthContext = createContext<AuthInfo | null>(null)

export function useAuthInfo() {
  return useContext(AuthContext)
}

/**
 * Whether to offer controls that change things. Only a convenience: the
 * server refuses a read-only caller's changes whatever the page shows, so
 * while the answer is unknown, controls stay enabled rather than flicker.
 */
export function useCanWrite() {
  const principal = useContext(AuthContext)?.principal
  return !principal || principal.role === 'admin'
}
