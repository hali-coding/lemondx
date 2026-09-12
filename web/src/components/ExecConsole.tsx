import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'

interface Entry {
  command: string
  stdout: string
  stderr: string
  exitCode: number
}

export function ExecConsole({ name, running }: { name: string; running: boolean }) {
  const [entries, setEntries] = useState<Entry[]>([])
  const [command, setCommand] = useState('')
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<string[]>([])
  const [historyIndex, setHistoryIndex] = useState(-1)
  const output = useRef<HTMLDivElement>(null)

  // Keep the newest output in view.
  useEffect(() => {
    output.current?.scrollTo({ top: output.current.scrollHeight })
  }, [entries, busy])

  async function run(event: React.FormEvent) {
    event.preventDefault()
    const trimmed = command.trim()
    if (!trimmed || busy) return
    setBusy(true)
    setHistory((h) => [...h, trimmed])
    setHistoryIndex(-1)
    setCommand('')
    try {
      const result = await api.exec(name, trimmed)
      setEntries((current) => [...current, {
        command: trimmed,
        stdout: result.stdout,
        stderr: result.stderr,
        exitCode: result.exit_code,
      }])
    } catch (cause) {
      setEntries((current) => [...current, {
        command: trimmed, stdout: '', stderr: (cause as Error).message, exitCode: -1,
      }])
    } finally {
      setBusy(false)
    }
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
    if (history.length === 0) return
    event.preventDefault()
    const next = event.key === 'ArrowUp'
      ? Math.min(history.length - 1, (historyIndex < 0 ? -1 : historyIndex) + 1)
      : historyIndex - 1
    if (next < 0) {
      setHistoryIndex(-1)
      setCommand('')
      return
    }
    setHistoryIndex(next)
    setCommand(history[history.length - 1 - next])
  }

  if (!running) {
    return (
      <p className="dim" style={{ fontSize: 13 }}>
        Start the container to run commands inside it.
      </p>
    )
  }

  return (
    <>
      <div className="console" ref={output}>
        {entries.map((entry, index) => (
          <div key={index}>
            <div className="cmd">$ {entry.command}</div>
            {entry.stdout && <div>{entry.stdout.replace(/\n$/, '')}</div>}
            {entry.stderr && <div className="err">{entry.stderr.replace(/\n$/, '')}</div>}
            {entry.exitCode !== 0 && (
              <div className="err">exited with code {entry.exitCode}</div>
            )}
          </div>
        ))}
        {busy && <div className="dim">running…</div>}
      </div>
      <form className="console-form" onSubmit={run}>
        <input
          className="input"
          value={command}
          onChange={(event) => setCommand(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder="uname -a"
          aria-label={`Command to run in ${name}`}
          autoComplete="off"
          spellCheck={false}
          disabled={busy}
        />
        <button className="btn btn-primary" type="submit" disabled={busy || !command.trim()}>
          Run
        </button>
      </form>
      <p className="hint" style={{ marginTop: 7 }}>
        Runs via <span className="mono">sh -c</span>, non-interactively. For a real shell use{' '}
        <span className="mono">./lemondx shell {name}</span>.
      </p>
    </>
  )
}
