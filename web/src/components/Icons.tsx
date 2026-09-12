/** Inline 16px stroke icons — keeps the bundle free of an icon dependency. */
type Props = { size?: number }

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.9,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
})

export const PlayIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><path d="M7 4.5v15l12-7.5z" /></svg>
)

export const StopIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><rect x="6" y="6" width="12" height="12" rx="1.5" /></svg>
)

export const RestartIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 3v6h-6" />
  </svg>
)

export const PauseIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><path d="M9 5v14M15 5v14" /></svg>
)

export const TrashIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M4 7h16M10 11v6M14 11v6" />
    <path d="M6 7l1 13h10l1-13M9 7V4h6v3" />
  </svg>
)

export const PlusIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><path d="M12 5v14M5 12h14" /></svg>
)

export const CloseIcon = ({ size = 16 }: Props) => (
  <svg {...base(size)}><path d="M6 6l12 12M18 6L6 18" /></svg>
)

export const SunIcon = ({ size = 16 }: Props) => (
  <svg {...base(size)}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
)

export const MoonIcon = ({ size = 16 }: Props) => (
  <svg {...base(size)}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>
)

export const CameraIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M3 8h3l2-2.5h8L18 8h3v11H3z" />
    <circle cx="12" cy="13" r="3.5" />
  </svg>
)

export const RefreshIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M3 12a9 9 0 0 1 15.3-6.4L21 8" />
    <path d="M21 3v5h-5M21 12a9 9 0 0 1-15.3 6.4L3 16" />
    <path d="M3 21v-5h5" />
  </svg>
)

export const BoxIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M21 8l-9-5-9 5 9 5 9-5zM3 8v8l9 5 9-5V8" />
  </svg>
)
