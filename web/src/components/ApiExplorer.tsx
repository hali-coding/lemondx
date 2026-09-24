import { useState } from 'react'
import type { ApiCall } from '../lib/api'
import { LANGUAGES, TOKEN_VAR, snippet } from '../lib/snippets'
import type { Language } from '../lib/snippets'
import { useAuthInfo } from '../hooks/useAuth'
import { CopyButton } from './CopyButton'

const LANGUAGE_KEY = 'lemondx-api-language'

function storedLanguage(): Language {
  try {
    const saved = localStorage.getItem(LANGUAGE_KEY)
    if (LANGUAGES.some((l) => l.id === saved)) return saved as Language
  } catch { /* storage refused: the default will do */ }
  return 'curl'
}

/**
 * What the dialog beside it would send, as code. It follows the form as it is
 * filled in, so the request shown is the request the button makes.
 */
export function ApiExplorer({ call }: { call: ApiCall | null }) {
  const [language, setLanguage] = useState<Language>(storedLanguage)
  const auth = useAuthInfo()

  function choose(next: Language) {
    setLanguage(next)
    try {
      localStorage.setItem(LANGUAGE_KEY, next)
    } catch { /* remembered for this dialog only */ }
  }

  // A cluster member wants a token from other hosts even with auth off, and
  // says so by listing the method; a snippet is usually run from elsewhere.
  const needsToken = !!auth && (auth.enabled || auth.methods.includes('token'))
  const code = call && snippet(language, call, {
    origin: window.location.origin,
    auth: needsToken,
  })
  const background = !!call && typeof call.body === 'object' && call.body !== null
    && (call.body as { background?: unknown }).background === true

  return (
    <aside className="api-explorer" aria-label="API explorer">
      <div className="api-explorer-head">
        <h3>API explorer</h3>
        <div className="stack-segmented" role="radiogroup" aria-label="Language">
          {LANGUAGES.map((item) => (
            <button key={item.id} type="button" role="radio"
              aria-checked={language === item.id} onClick={() => choose(item.id)}>
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {call ? (
        <>
          <div className="api-explorer-endpoint">
            <span className={`api-method api-method-${call.method.toLowerCase()}`}>
              {call.method}
            </span>
            <span className="mono">/api{call.path}</span>
          </div>
          <div className="api-explorer-code">
            <pre className="mono">{code}</pre>
            <CopyButton text={code ?? ''} label={`${language} example`} />
          </div>
          <ul className="api-explorer-notes">
            {needsToken && (
              <li>
                Set <code>{TOKEN_VAR}</code> to an API token from the Access tab. A token
                carries its own role, and one without the access this needs is refused.
              </li>
            )}
            {!!call.secrets?.length && (
              <li>Secrets are shown as <code>&lt;name&gt;</code>: put the real value there.</li>
            )}
            {background && (
              <li>
                <code>background: true</code> answers at once with a run record, as the UI
                needs; leave it out and the call waits until the work is done.
              </li>
            )}
            <li>
              Success is <code>{'{"data": …}'}</code>; a failure is{' '}
              <code>{'{"error": "…"}'}</code> with an HTTP error status.
            </li>
          </ul>
        </>
      ) : (
        <p className="hint">Fill in the form to see the request it makes.</p>
      )}
    </aside>
  )
}
