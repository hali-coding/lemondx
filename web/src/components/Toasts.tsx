import type { Notice, Toast } from '../hooks/useToasts'
import { CloseIcon } from './Icons'

interface Props {
  toasts: Toast[]
  onDismiss: (id: number) => void
  /** Kept in one box below the toasts, so the two stack rather than one
      covering the other. */
  notices?: Notice[]
  onCloseNotices?: (keys: string[]) => void
}

const SEVERITY: Notice['kind'][] = ['info', 'warn', 'error']

export function Toasts({ toasts, onDismiss, notices = [], onCloseNotices }: Props) {
  if (toasts.length === 0 && notices.length === 0) return null
  // The box takes the colour of the worst thing in it.
  const worst = notices.reduce<Notice['kind']>(
    (kind, n) => SEVERITY.indexOf(n.kind) > SEVERITY.indexOf(kind) ? n.kind : kind, 'info')
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast-${toast.kind}`}>
          <div className="toast-body">
            <div className="toast-title">{toast.title}</div>
            {toast.detail && <div className="toast-detail">{toast.detail}</div>}
          </div>
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => onDismiss(toast.id)}
            aria-label="Dismiss notification"
          >
            <CloseIcon size={14} />
          </button>
        </div>
      ))}
      {notices.length > 0 && (
        <div className={`toast toast-${worst}`}>
          <div className="toast-body notices">
          {notices.map((notice) => (
            <div key={notice.key} className={`notice notice-${notice.kind}`}>
              <div className="toast-title">{notice.title}</div>
              {(notice.detail || notice.action) && (
                <div className="toast-detail">
                  {notice.detail}
                  {notice.action && (
                    <>
                      {notice.detail && ' '}
                      <button type="button" className="link-btn" onClick={notice.action.onClick}>
                        {notice.action.label}
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
          </div>
          {onCloseNotices && (
            <button
              className="btn btn-ghost btn-icon"
              onClick={() => onCloseNotices(notices.map((n) => n.key))}
              aria-label="Close notices"
            >
              <CloseIcon size={14} />
            </button>
          )}
        </div>
      )}
    </div>
  )
}
