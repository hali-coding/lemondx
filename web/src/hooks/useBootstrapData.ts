import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapModule, BootstrapProfile, SshKey } from '../lib/types'

/**
 * Loads the module catalogue, saved profiles, and the host's public keys.
 *
 * ``node`` is the host they should come from, when it is not this one:
 * bootstrapping a container runs modules on *its* node and installs keys that
 * node can see, so offering this one's would be offering the wrong thing.
 */
export function useBootstrapData(node?: string) {
  const [modules, setModules] = useState<BootstrapModule[]>([])
  const [profiles, setProfiles] = useState<BootstrapProfile[]>([])
  const [hostKeys, setHostKeys] = useState<SshKey[]>([])
  const [loading, setLoading] = useState(true)

  const reloadProfiles = useCallback(() => {
    api.bootstrapProfiles(undefined, node).then(setProfiles).catch(() => {})
  }, [node])

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      api.modules(controller.signal, node).catch(() => [] as BootstrapModule[]),
      api.sshKeys(controller.signal, node).catch(() => [] as SshKey[]),
      api.bootstrapProfiles(controller.signal, node).catch(() => [] as BootstrapProfile[]),
    ])
      .then(([loadedModules, loadedKeys, loadedProfiles]) => {
        setModules(loadedModules)
        setHostKeys(loadedKeys)
        setProfiles(loadedProfiles)
        setLoading(false)
      })
      .catch(() => setLoading(false))
    return () => controller.abort()
  }, [node])

  return { modules, profiles, hostKeys, loading, reloadProfiles }
}
