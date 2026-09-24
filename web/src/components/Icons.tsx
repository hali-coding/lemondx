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

export const PencilIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
  </svg>
)

export const CopyIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h8" />
  </svg>
)

export const CheckIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><path d="M5 12.5l4.5 4.5L19 7" /></svg>
)

export const ChevronRightIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><path d="M9 6l6 6-6 6" /></svg>
)

export const ChevronLeftIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><path d="M15 6l-6 6 6 6" /></svg>
)

export const TerminalIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M5 8l4 4-4 4M12 17h7" />
  </svg>
)

export const CodeIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}><path d="M8 7l-5 5 5 5M16 7l5 5-5 5M13.5 5l-3 14" /></svg>
)

export const ServerIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <rect x="3" y="4" width="18" height="7" rx="1.5" />
    <rect x="3" y="13" width="18" height="7" rx="1.5" />
    <path d="M7 7.5h.01M7 16.5h.01" />
  </svg>
)

export const UploadIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" />
    <path d="M4 17v2.5A1.5 1.5 0 0 0 5.5 21h13a1.5 1.5 0 0 0 1.5-1.5V17" />
  </svg>
)

export const KeyIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <circle cx="8" cy="15" r="4" />
    <path d="M10.85 12.15 20 3m-3 3 2.5 2.5M14 9l2.5 2.5" />
  </svg>
)

export const ExternalIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M14 4h6v6M20 4l-8.5 8.5" />
    <path d="M18 14.5V19a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 19V8a1.5 1.5 0 0 1 1.5-1.5H10" />
  </svg>
)

export const LogoutIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M15 5V4a1.5 1.5 0 0 0-1.5-1.5h-8A1.5 1.5 0 0 0 4 4v16a1.5 1.5 0 0 0 1.5 1.5h8A1.5 1.5 0 0 0 15 20v-1" />
    <path d="M11 12h10m0 0-3.5-3.5M21 12l-3.5 3.5" />
  </svg>
)

export const EjectIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M12 3.5 4.5 13.5h15z" />
    <path d="M5 18.5h14" />
  </svg>
)

export const ScalesIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M12 4v16M7 20h10M5 8h14l-3-3M5 8l-2.5 5.5a3 3 0 0 0 5 0z" />
    <path d="M19 8l2.5 5.5a3 3 0 0 1-5 0z" />
  </svg>
)

export const StackIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M12 3 3 7.5l9 4.5 9-4.5z" />
    <path d="m3 12 9 4.5 9-4.5M3 16.5 12 21l9-4.5" />
  </svg>
)

export const ClockIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </svg>
)

export const HeartIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.3 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z" />
    <path d="M7 12.5h2.5l1.2-2.2 2 4.2 1.3-2h3" />
  </svg>
)

export const GripIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01" strokeWidth={3} />
  </svg>
)

export const TemplateIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <path d="M3 9h18M9 21V9" />
  </svg>
)

export const GaugeIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M3.5 17a9 9 0 1 1 17 0" />
    <path d="M12 13l4-5" />
    <circle cx="12" cy="14" r="1.5" />
  </svg>
)

export const DatabaseIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <ellipse cx="12" cy="5.5" rx="8" ry="3" />
    <path d="M4 5.5v13c0 1.66 3.58 3 8 3s8-1.34 8-3v-13" />
    <path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3" />
  </svg>
)

export const NetworkIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <rect x="9" y="2.5" width="6" height="5" rx="1" />
    <rect x="2.5" y="16.5" width="6" height="5" rx="1" />
    <rect x="15.5" y="16.5" width="6" height="5" rx="1" />
    <path d="M12 7.5v4.5M5.5 16.5V12h13v4.5" />
  </svg>
)

export const PuzzleIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M9 4.5a2 2 0 1 1 4 0V6h4a1 1 0 0 1 1 1v4h-1.5a2 2 0 1 0 0 4H18v4a1 1 0 0 1-1 1h-4v-1.5a2 2 0 1 0-4 0V20H5a1 1 0 0 1-1-1v-4h1.5a2 2 0 1 0 0-4H4V7a1 1 0 0 1 1-1h4z" />
  </svg>
)

export const ShieldIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z" />
    <path d="M9 12l2 2 4-4" />
  </svg>
)

export const MenuIcon = ({ size = 16 }: Props) => (
  <svg {...base(size)}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
)

export const HelpIcon = ({ size = 13 }: Props) => (
  <svg {...base(size)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.5 9.5a2.5 2.5 0 0 1 4.8.9c0 1.6-2.3 2.2-2.3 3.6M12 17h.01" />
  </svg>
)

export const HomeIcon = ({ size = 15 }: Props) => (
  <svg {...base(size)}>
    <path d="M3.5 10.5 12 4l8.5 6.5" />
    <path d="M5.5 9v10.5h13V9M10 19.5v-5h4v5" />
  </svg>
)

export const WrenchIcon = ({ size = 13 }: Props) => (
  <svg {...base(size)}>
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z" />
  </svg>
)
