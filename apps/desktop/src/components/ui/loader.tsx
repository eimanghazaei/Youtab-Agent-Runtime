import type { ComponentProps } from 'react'

import { cn } from '@/lib/utils'

import { SimorghOrb } from './simorgh-orb'

// Kept for API compatibility (exported via src/sdk). Call sites still pass a
// `type`, but every loader now renders the single Youtab / Simorgh brand orb;
// the value is accepted and ignored.
export const LOADER_TYPES = [
  'original-thinking',
  'thinking-five',
  'thinking-nine',
  'rose-orbit',
  'rose-curve',
  'rose-two',
  'rose-three',
  'rose-four',
  'lissajous-drift',
  'lemniscate-bloom',
  'hypotrochoid-loop',
  'three-petal-spiral',
  'four-petal-spiral',
  'five-petal-spiral',
  'six-petal-spiral',
  'butterfly-phase',
  'cardioid-glow',
  'cardioid-heart',
  'heart-wave',
  'spiral-search',
  'fourier-flow'
] as const

export type LoaderType = (typeof LOADER_TYPES)[number]

interface LoaderProps extends Omit<ComponentProps<'div'>, 'children'> {
  label?: string
  /** Accepted for API compatibility; the brand orb ignores it. */
  pathSteps?: number
  /** Accepted for API compatibility; the brand orb ignores it. */
  strokeScale?: number
  /** Accepted for API compatibility; the brand orb ignores it. */
  type?: LoaderType
}

/**
 * Youtab / Simorgh brand loader. Renders the single brand orb
 * (`<SimorghOrb>`), slowly rotating so it still reads as an active loading
 * indicator. `className` controls both size (e.g. `size-10`) and color (via
 * `currentColor`). Honors `prefers-reduced-motion`.
 */
export function Loader({
  label = 'Loading',
  className,
  role = 'status',
  'aria-label': ariaLabel,
  // Accepted for compatibility with the previous particle loader; ignored.
  pathSteps: _pathSteps,
  strokeScale: _strokeScale,
  type: _type,
  ...props
}: LoaderProps) {
  return (
    <div
      {...props}
      aria-label={ariaLabel ?? label}
      className={cn('inline-flex items-center justify-center', className)}
      role={role}
    >
      <SimorghOrb aria-hidden="true" className="size-full [animation-duration:6s] motion-safe:animate-spin" />
    </div>
  )
}
