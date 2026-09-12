import type { BootstrapResult } from '../lib/types'

export function BootstrapLog({ result }: { result: BootstrapResult }) {
  return (
    <div className="console" style={{ maxHeight: 300 }}>
      {result.modules.map((module) => {
        const ok = module.exit_code === 0
        const body = `${module.stdout ?? ''}${module.stderr ?? ''}`.replace(/\n$/, '')
        return (
          <div key={module.id}>
            <div className={ok ? 'code-ok' : 'err'}>
              {ok ? '✓' : '✗'} {module.name} ({module.duration}s)
            </div>
            {body && <div className={ok ? undefined : 'err'}>{body}</div>}
            {!ok && <div className="err">exited with code {module.exit_code}</div>}
          </div>
        )
      })}
      <div className={result.ok ? 'code-ok' : 'err'}>
        {result.ok ? 'bootstrap finished' : 'bootstrap failed'}
      </div>
    </div>
  )
}
