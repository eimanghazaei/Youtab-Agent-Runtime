import type { SVGProps } from 'react'

/**
 * Youtab / Simorgh "working" orb — ~90 particles arranged on tilted orbits, the
 * active-work sibling of the calmer {@link SimorghOrb} sash. Static markup;
 * theme-aware (`fill: currentColor`) and scalable via `className` (no intrinsic
 * width/height so it fills its box). Decorative by default (`aria-hidden`);
 * wrap it in the spin idiom (`[animation-duration:…] motion-safe:animate-spin`)
 * for the streaming / thinking indicator.
 */
export function SimorghOrbWorking({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg aria-hidden="true" className={className} viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" {...props}>
      <g fill="currentColor">
        <circle cx="53.28" cy="27.59" opacity="0.742" r="1.67" />
        <circle cx="48.59" cy="25.82" opacity="0.857" r="1.35" />
        <circle cx="41.90" cy="24.80" opacity="0.939" r="1.01" />
        <circle cx="34.01" cy="24.64" opacity="0.977" r="1.05" />
        <circle cx="25.88" cy="25.37" opacity="0.968" r="1.48" />
        <circle cx="18.49" cy="26.90" opacity="0.912" r="1.87" />
        <circle cx="12.73" cy="29.05" opacity="0.816" r="1.82" />
        <circle cx="9.29" cy="31.55" opacity="0.691" r="1.35" />
        <circle cx="8.59" cy="34.11" opacity="0.553" r="0.86" />
        <circle cx="10.72" cy="36.41" opacity="0.418" r="0.69" />
        <circle cx="15.41" cy="38.18" opacity="0.303" r="0.85" />
        <circle cx="22.10" cy="39.20" opacity="0.221" r="1.09" />
        <circle cx="29.99" cy="39.36" opacity="0.183" r="1.16" />
        <circle cx="38.12" cy="38.63" opacity="0.192" r="1.00" />
        <circle cx="45.51" cy="37.10" opacity="0.248" r="0.74" />
        <circle cx="51.27" cy="34.95" opacity="0.344" r="0.64" />
        <circle cx="54.71" cy="32.45" opacity="0.469" r="0.87" />
        <circle cx="55.41" cy="29.89" opacity="0.607" r="1.37" />
        <circle cx="36.53" cy="13.21" opacity="0.820" r="1.32" />
        <circle cx="28.69" cy="11.77" opacity="0.787" r="0.92" />
        <circle cx="21.26" cy="12.77" opacity="0.729" r="0.91" />
        <circle cx="15.12" cy="16.10" opacity="0.653" r="1.23" />
        <circle cx="11.01" cy="21.34" opacity="0.568" r="1.51" />
        <circle cx="9.44" cy="27.86" opacity="0.485" r="1.46" />
        <circle cx="10.59" cy="34.89" opacity="0.413" r="1.11" />
        <circle cx="14.32" cy="41.56" opacity="0.361" r="0.74" />
        <circle cx="20.18" cy="47.09" opacity="0.336" r="0.65" />
        <circle cx="27.47" cy="50.79" opacity="0.340" r="0.88" />
        <circle cx="35.31" cy="52.23" opacity="0.373" r="1.24" />
        <circle cx="42.74" cy="51.23" opacity="0.431" r="1.43" />
        <circle cx="48.88" cy="47.90" opacity="0.507" r="1.30" />
        <circle cx="52.99" cy="42.66" opacity="0.592" r="0.97" />
        <circle cx="54.56" cy="36.14" opacity="0.675" r="0.81" />
        <circle cx="53.41" cy="29.11" opacity="0.747" r="1.05" />
        <circle cx="49.68" cy="22.44" opacity="0.799" r="1.55" />
        <circle cx="43.82" cy="16.91" opacity="0.824" r="1.86" />
        <circle cx="21.15" cy="11.10" opacity="0.585" r="0.81" />
        <circle cx="14.66" cy="16.06" opacity="0.579" r="0.82" />
        <circle cx="10.26" cy="22.95" opacity="0.572" r="1.16" />
        <circle cx="8.48" cy="30.92" opacity="0.567" r="1.51" />
        <circle cx="9.54" cy="39.03" opacity="0.563" r="1.55" />
        <circle cx="13.31" cy="46.29" opacity="0.561" r="1.24" />
        <circle cx="19.33" cy="51.83" opacity="0.561" r="0.86" />
        <circle cx="26.88" cy="54.97" opacity="0.564" r="0.77" />
        <circle cx="35.05" cy="55.35" opacity="0.569" r="1.04" />
        <circle cx="42.85" cy="52.90" opacity="0.575" r="1.44" />
        <circle cx="49.34" cy="47.94" opacity="0.581" r="1.60" />
        <circle cx="53.74" cy="41.05" opacity="0.588" r="1.37" />
        <circle cx="55.52" cy="33.08" opacity="0.593" r="0.97" />
        <circle cx="54.46" cy="24.97" opacity="0.597" r="0.77" />
        <circle cx="50.69" cy="17.71" opacity="0.599" r="0.96" />
        <circle cx="44.67" cy="12.17" opacity="0.599" r="1.36" />
        <circle cx="37.12" cy="9.03" opacity="0.596" r="1.61" />
        <circle cx="28.95" cy="8.65" opacity="0.591" r="1.47" />
        <circle cx="12.46" cy="23.65" opacity="0.761" r="0.93" />
        <circle cx="10.57" cy="31.60" opacity="0.754" r="1.31" />
        <circle cx="11.27" cy="39.60" opacity="0.726" r="1.68" />
        <circle cx="14.47" cy="46.68" opacity="0.681" r="1.67" />
        <circle cx="19.78" cy="51.99" opacity="0.623" r="1.29" />
        <circle cx="26.56" cy="54.89" opacity="0.560" r="0.86" />
        <circle cx="34.01" cy="55.03" opacity="0.499" r="0.73" />
        <circle cx="41.21" cy="52.39" opacity="0.449" r="0.96" />
        <circle cx="47.29" cy="47.29" opacity="0.414" r="1.28" />
        <circle cx="51.54" cy="40.35" opacity="0.399" r="1.39" />
        <circle cx="53.43" cy="32.40" opacity="0.406" r="1.20" />
        <circle cx="52.73" cy="24.40" opacity="0.434" r="0.87" />
        <circle cx="49.53" cy="17.32" opacity="0.479" r="0.71" />
        <circle cx="44.22" cy="12.01" opacity="0.537" r="0.92" />
        <circle cx="37.44" cy="9.11" opacity="0.600" r="1.36" />
        <circle cx="29.99" cy="8.97" opacity="0.661" r="1.68" />
        <circle cx="22.79" cy="11.61" opacity="0.711" r="1.59" />
        <circle cx="16.71" cy="16.71" opacity="0.746" r="1.20" />
        <circle cx="9.86" cy="39.14" opacity="0.645" r="1.22" />
        <circle cx="12.00" cy="44.20" opacity="0.537" r="1.48" />
        <circle cx="16.56" cy="47.80" opacity="0.434" r="1.41" />
        <circle cx="22.98" cy="49.48" opacity="0.349" r="1.05" />
        <circle cx="30.48" cy="49.06" opacity="0.292" r="0.70" />
        <circle cx="38.17" cy="46.58" opacity="0.269" r="0.61" />
        <circle cx="45.12" cy="42.35" opacity="0.284" r="0.84" />
        <circle cx="50.48" cy="36.86" opacity="0.335" r="1.20" />
        <circle cx="53.62" cy="30.79" opacity="0.415" r="1.41" />
        <circle cx="54.14" cy="24.86" opacity="0.515" r="1.30" />
        <circle cx="52.00" cy="19.80" opacity="0.623" r="0.99" />
        <circle cx="47.44" cy="16.20" opacity="0.726" r="0.84" />
        <circle cx="41.02" cy="14.52" opacity="0.811" r="1.10" />
        <circle cx="33.52" cy="14.94" opacity="0.868" r="1.61" />
        <circle cx="25.83" cy="17.42" opacity="0.891" r="1.93" />
        <circle cx="18.88" cy="21.65" opacity="0.876" r="1.76" />
        <circle cx="13.52" cy="27.14" opacity="0.825" r="1.26" />
        <circle cx="10.38" cy="33.21" opacity="0.745" r="0.87" />
      </g>
    </svg>
  )
}
