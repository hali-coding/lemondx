import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapModule, SshKey } from '../lib/types'

/** Loads the module catalogue and the host's public keys, once. */
export function useBootstrapData() {
  const [modules, setModules] = useState<BootstrapModule[]>([])
  const [hostKeys, setHostKeys] = useState<SshKey[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      api.modules(controller.signal).catch(() => [] as BootstrapModule[]),
      api.sshKeys(controller.signal).catch(() => [] as SshKey[]),
    ])
      .then(([loadedModules, loadedKeys]) => {
        setModules(loadedModules)
        setHostKeys(loadedKeys)
        setLoading(false)
      })
      .catch(() => setLoading(false))
    return () => controller.abort()
  }, [])

  return { modules, hostKeys, loading }
}
