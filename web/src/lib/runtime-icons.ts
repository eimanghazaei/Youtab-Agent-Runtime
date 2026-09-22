// Shared icon registry for the Agent Runtime feature surfaces (Run / Model /
// Connections / Files / Enterprise / Governance / History and any view a future
// session adds under them).
//
// DESIGN-SYSTEM CONTRACT: feature views MUST import icons from this registry,
// never directly from `lucide-react` / `@tabler/icons-react`. The lint guard in
// `web/eslint.config.js` fails the build on a direct icon import inside the
// runtime feature dir. Adding a new icon here (one line) is how a session
// introduces one without fragmenting the icon set.
export {
  AlertTriangle,
  Building2,
  Cpu,
  FolderOpen,
  Play,
  Plug,
  ScrollText,
  ShieldCheck,
  Square,
  Terminal
} from 'lucide-react'

export type { LucideIcon } from 'lucide-react'
