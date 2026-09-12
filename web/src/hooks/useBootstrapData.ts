import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapModule, BootstrapProfile, SshKey } from '../lib/types'

/** Loads the module catalogue, saved profiles, and the host's public keys. */
export function useBootstrapData() {
  const [modules, setModules] = useState<BootstrapModule[]>([])
  const [profiles, setProfiles] = useState<BootstrapProfile[]>([])
  const [hostKeys, setHostKeys] = useState<SshKey[]>([])
  const [loading, setLoading] = useState(true)

  const reloadProfiles = useCallback(() => {
    api.bootstrapProfiles().then(setProfiles).catch(() => {})
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      api.modules(controller.signal).catch(() => [] as BootstrapModule[]),
      api.sshKeys(controller.signal).catch(() => [] as SshKey[]),
      api.bootstrapProfiles(controller.signal).catch(() => [] as BootstrapProfile[]),
    ])
      .then(([loadedModules, loadedKeys, loadedProfiles]) => {
        setModules(loadedModules)
        setHostKeys(loadedKeys)
        setProfiles(loadedProfiles)
        setLoading(false)
      })
      .catch(() => setLoading(false))
    return () => controller.abort()
  }, [])

  return { modules, profiles, hostKeys, loading, reloadProfiles }
}
