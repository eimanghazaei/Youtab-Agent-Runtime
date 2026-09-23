import type { SVGProps } from 'react'

/**
 * Youtab / Simorgh brand orb — an undulating sash of fading dots. Static,
 * theme-aware (fill: currentColor), and scalable via `className` (no intrinsic
 * width/height so it fills its box). Used as the app's single brand mark; the
 * `Loader` wraps it with a slow spin for loading states.
 */
export function SimorghOrb({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-label="Simorgh"
      className={className}
      role="img"
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
      {...props}
    >
      <title>Simorgh</title>
      <g fill="currentColor">
        <circle cx="53.88" cy="39.02" opacity="0.704" r="1.47" />
        <circle cx="52.44" cy="42.81" opacity="0.696" r="1.47" />
        <circle cx="50.41" cy="46.29" opacity="0.685" r="1.45" />
        <circle cx="52.50" cy="39.74" opacity="0.754" r="1.38" />
        <circle cx="51.03" cy="43.49" opacity="0.741" r="1.36" />
        <circle cx="49.00" cy="46.90" opacity="0.724" r="1.35" />
        <circle cx="50.99" cy="39.62" opacity="0.801" r="1.26" />
        <circle cx="49.64" cy="43.37" opacity="0.785" r="1.25" />
        <circle cx="47.79" cy="46.80" opacity="0.764" r="1.23" />
        <circle cx="49.31" cy="38.66" opacity="0.848" r="1.15" />
        <circle cx="48.22" cy="42.48" opacity="0.831" r="1.14" />
        <circle cx="46.66" cy="45.99" opacity="0.807" r="1.12" />
        <circle cx="47.35" cy="36.90" opacity="0.892" r="1.04" />
        <circle cx="46.58" cy="40.82" opacity="0.876" r="1.04" />
        <circle cx="45.40" cy="44.48" opacity="0.852" r="1.02" />
        <circle cx="44.98" cy="34.46" opacity="0.931" r="0.96" />
        <circle cx="44.56" cy="38.47" opacity="0.920" r="0.95" />
        <circle cx="43.79" cy="42.30" opacity="0.899" r="0.94" />
        <circle cx="42.17" cy="31.49" opacity="0.961" r="0.90" />
        <circle cx="42.06" cy="35.57" opacity="0.956" r="0.90" />
        <circle cx="41.66" cy="39.55" opacity="0.941" r="0.90" />
        <circle cx="39.02" cy="28.28" opacity="0.976" r="0.88" />
        <circle cx="39.10" cy="32.35" opacity="0.981" r="0.89" />
        <circle cx="38.98" cy="36.42" opacity="0.974" r="0.88" />
        <circle cx="35.74" cy="25.10" opacity="0.977" r="0.90" />
        <circle cx="35.88" cy="29.10" opacity="0.991" r="0.91" />
        <circle cx="35.90" cy="33.19" opacity="0.994" r="0.91" />
        <circle cx="32.58" cy="22.25" opacity="0.964" r="0.96" />
        <circle cx="32.62" cy="26.11" opacity="0.987" r="0.97" />
        <circle cx="32.64" cy="30.15" opacity="0.999" r="0.98" />
        <circle cx="29.71" cy="19.93" opacity="0.941" r="1.05" />
        <circle cx="29.52" cy="23.63" opacity="0.971" r="1.07" />
        <circle cx="29.40" cy="27.57" opacity="0.990" r="1.08" />
        <circle cx="27.17" cy="18.27" opacity="0.915" r="1.16" />
        <circle cx="26.68" cy="21.82" opacity="0.949" r="1.19" />
        <circle cx="26.33" cy="25.65" opacity="0.973" r="1.20" />
        <circle cx="24.87" cy="17.32" opacity="0.889" r="1.30" />
        <circle cx="24.04" cy="20.75" opacity="0.924" r="1.32" />
        <circle cx="23.45" cy="24.51" opacity="0.950" r="1.34" />
        <circle cx="22.63" cy="17.06" opacity="0.866" r="1.44" />
        <circle cx="21.51" cy="20.47" opacity="0.900" r="1.46" />
        <circle cx="20.69" cy="24.21" opacity="0.925" r="1.49" />
        <circle cx="20.27" cy="17.52" opacity="0.846" r="1.57" />
        <circle cx="18.94" cy="20.98" opacity="0.876" r="1.60" />
        <circle cx="17.99" cy="24.75" opacity="0.897" r="1.62" />
        <circle cx="17.72" cy="18.68" opacity="0.825" r="1.69" />
        <circle cx="16.32" cy="22.26" opacity="0.849" r="1.72" />
        <circle cx="15.37" cy="26.12" opacity="0.866" r="1.74" />
        <circle cx="15.05" cy="20.53" opacity="0.801" r="1.78" />
        <circle cx="13.74" cy="24.28" opacity="0.818" r="1.80" />
        <circle cx="12.95" cy="28.24" opacity="0.828" r="1.81" />
        <circle cx="12.46" cy="23.02" opacity="0.768" r="1.83" />
        <circle cx="11.41" cy="26.92" opacity="0.778" r="1.84" />
        <circle cx="10.95" cy="30.98" opacity="0.783" r="1.85" />
        <circle cx="10.26" cy="25.99" opacity="0.726" r="1.83" />
        <circle cx="9.62" cy="30.02" opacity="0.731" r="1.83" />
        <circle cx="9.63" cy="34.10" opacity="0.731" r="1.83" />
        <circle cx="8.74" cy="29.20" opacity="0.676" r="1.77" />
        <circle cx="8.61" cy="33.29" opacity="0.676" r="1.77" />
        <circle cx="9.16" cy="37.33" opacity="0.674" r="1.77" />
        <circle cx="8.05" cy="32.38" opacity="0.620" r="1.68" />
        <circle cx="8.46" cy="36.44" opacity="0.619" r="1.68" />
        <circle cx="9.54" cy="40.38" opacity="0.617" r="1.67" />
        <circle cx="8.17" cy="35.22" opacity="0.563" r="1.55" />
        <circle cx="9.06" cy="39.20" opacity="0.563" r="1.55" />
        <circle cx="10.61" cy="42.98" opacity="0.564" r="1.55" />
        <circle cx="8.94" cy="37.48" opacity="0.507" r="1.40" />
        <circle cx="10.19" cy="41.37" opacity="0.511" r="1.40" />
        <circle cx="12.06" cy="44.98" opacity="0.517" r="1.41" />
        <circle cx="10.12" cy="39.02" opacity="0.456" r="1.24" />
        <circle cx="11.56" cy="42.81" opacity="0.464" r="1.25" />
        <circle cx="13.59" cy="46.29" opacity="0.475" r="1.26" />
        <circle cx="11.50" cy="39.74" opacity="0.406" r="1.08" />
        <circle cx="12.97" cy="43.49" opacity="0.419" r="1.09" />
        <circle cx="15.00" cy="46.90" opacity="0.436" r="1.11" />
        <circle cx="13.01" cy="39.62" opacity="0.359" r="0.93" />
        <circle cx="14.36" cy="43.37" opacity="0.375" r="0.94" />
        <circle cx="16.21" cy="46.80" opacity="0.396" r="0.96" />
        <circle cx="14.69" cy="38.66" opacity="0.312" r="0.79" />
        <circle cx="15.78" cy="42.48" opacity="0.329" r="0.80" />
        <circle cx="17.34" cy="45.99" opacity="0.353" r="0.82" />
        <circle cx="16.65" cy="36.90" opacity="0.268" r="0.67" />
        <circle cx="17.42" cy="40.82" opacity="0.284" r="0.68" />
        <circle cx="18.60" cy="44.48" opacity="0.308" r="0.70" />
        <circle cx="19.02" cy="34.46" opacity="0.229" r="0.59" />
        <circle cx="19.44" cy="38.47" opacity="0.240" r="0.59" />
        <circle cx="20.21" cy="42.30" opacity="0.261" r="0.60" />
        <circle cx="21.83" cy="31.49" opacity="0.199" r="0.53" />
        <circle cx="21.94" cy="35.57" opacity="0.204" r="0.53" />
        <circle cx="22.34" cy="39.55" opacity="0.219" r="0.54" />
        <circle cx="24.98" cy="28.28" opacity="0.184" r="0.50" />
        <circle cx="24.90" cy="32.35" opacity="0.179" r="0.50" />
        <circle cx="25.02" cy="36.42" opacity="0.186" r="0.51" />
        <circle cx="28.26" cy="25.10" opacity="0.183" r="0.51" />
        <circle cx="28.12" cy="29.10" opacity="0.169" r="0.51" />
        <circle cx="28.10" cy="33.19" opacity="0.166" r="0.51" />
        <circle cx="31.42" cy="22.25" opacity="0.196" r="0.56" />
        <circle cx="31.38" cy="26.11" opacity="0.173" r="0.54" />
        <circle cx="31.36" cy="30.15" opacity="0.161" r="0.54" />
        <circle cx="34.29" cy="19.93" opacity="0.219" r="0.63" />
        <circle cx="34.48" cy="23.63" opacity="0.189" r="0.61" />
        <circle cx="34.60" cy="27.57" opacity="0.170" r="0.60" />
        <circle cx="36.83" cy="18.27" opacity="0.245" r="0.73" />
        <circle cx="37.32" cy="21.82" opacity="0.211" r="0.70" />
        <circle cx="37.67" cy="25.65" opacity="0.187" r="0.69" />
        <circle cx="39.13" cy="17.32" opacity="0.271" r="0.84" />
        <circle cx="39.96" cy="20.75" opacity="0.236" r="0.81" />
        <circle cx="40.55" cy="24.51" opacity="0.210" r="0.79" />
        <circle cx="41.37" cy="17.06" opacity="0.294" r="0.96" />
        <circle cx="42.49" cy="20.47" opacity="0.260" r="0.93" />
        <circle cx="43.31" cy="24.21" opacity="0.235" r="0.91" />
        <circle cx="43.73" cy="17.52" opacity="0.314" r="1.09" />
        <circle cx="45.06" cy="20.98" opacity="0.284" r="1.06" />
        <circle cx="46.01" cy="24.75" opacity="0.263" r="1.04" />
        <circle cx="46.28" cy="18.68" opacity="0.335" r="1.20" />
        <circle cx="47.68" cy="22.26" opacity="0.311" r="1.18" />
        <circle cx="48.63" cy="26.12" opacity="0.294" r="1.16" />
        <circle cx="48.95" cy="20.53" opacity="0.359" r="1.31" />
        <circle cx="50.26" cy="24.28" opacity="0.342" r="1.29" />
        <circle cx="51.05" cy="28.24" opacity="0.332" r="1.28" />
        <circle cx="51.54" cy="23.02" opacity="0.392" r="1.41" />
        <circle cx="52.59" cy="26.92" opacity="0.382" r="1.40" />
        <circle cx="53.05" cy="30.98" opacity="0.377" r="1.39" />
        <circle cx="53.74" cy="25.99" opacity="0.434" r="1.49" />
        <circle cx="54.38" cy="30.02" opacity="0.429" r="1.49" />
        <circle cx="54.37" cy="34.10" opacity="0.429" r="1.49" />
        <circle cx="55.26" cy="29.20" opacity="0.484" r="1.55" />
        <circle cx="55.39" cy="33.29" opacity="0.484" r="1.55" />
        <circle cx="54.84" cy="37.33" opacity="0.486" r="1.56" />
        <circle cx="55.95" cy="32.38" opacity="0.540" r="1.59" />
        <circle cx="55.54" cy="36.44" opacity="0.541" r="1.59" />
        <circle cx="54.46" cy="40.38" opacity="0.543" r="1.59" />
        <circle cx="55.83" cy="35.22" opacity="0.597" r="1.59" />
        <circle cx="54.94" cy="39.20" opacity="0.597" r="1.58" />
        <circle cx="53.39" cy="42.98" opacity="0.596" r="1.58" />
        <circle cx="55.06" cy="37.48" opacity="0.653" r="1.55" />
        <circle cx="53.81" cy="41.37" opacity="0.649" r="1.54" />
        <circle cx="51.94" cy="44.98" opacity="0.643" r="1.54" />
      </g>
    </svg>
  )
}
