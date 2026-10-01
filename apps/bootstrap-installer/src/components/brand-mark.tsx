import simorghOrbUrl from '../assets/simorgh-orb-working.svg'
import { cn } from '../lib/utils'

// The supplied SVG uses currentColor. A CSS mask keeps it tinted by the
// installer's existing theme tokens in both light and dark appearances.
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span className={cn('inline-flex size-14 shrink-0 items-center justify-center text-primary', className)} {...props}>
      <span
        aria-hidden="true"
        className="size-full bg-current"
        style={{
          WebkitMask: `url('${simorghOrbUrl}') center / contain no-repeat`,
          mask: `url('${simorghOrbUrl}') center / contain no-repeat`
        }}
      />
    </span>
  )
}
