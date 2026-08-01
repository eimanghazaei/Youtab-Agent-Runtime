'use client';
import { jsx as _jsx, Fragment as _Fragment, jsxs as _jsxs } from "react/jsx-runtime";
import { Glitch } from './glitch';
import { Greys } from './greys';
import { Lens } from './lens-layers';
import { Noise } from './noise';
import { Vignette } from './vignette';
export { BLEND_MODES } from './blend-modes';
export { Glitch } from './glitch';
export { Greys } from './greys';
export { Lens } from './lens-layers';
export { Noise } from './noise';
export { Vignette } from './vignette';
export { $lightMode, applyLens, lens0, lens5i, LENS_0, LENS_5I, LENSES, toggleLens } from './lens';
const LAYER = 'pointer-events-none fixed inset-0';
export function Overlays({ dark, initial }) {
    return (_jsxs(_Fragment, { children: [_jsx(Lens, { dark: dark, initial: initial }), _jsx(Noise, { className: LAYER, style: { zIndex: 101 } }), _jsx(Vignette, { className: LAYER, style: { zIndex: 99 } }), _jsx(Greys, { className: LAYER, style: { zIndex: 200 } }), _jsx(Glitch, { className: LAYER, style: { zIndex: 201 } })] }));
}
//# sourceMappingURL=index.js.map