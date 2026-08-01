'use client';
import { useEffect } from 'react';
export function useCssVarDims(name, ref) {
    useEffect(() => {
        if (!ref.current) {
            return;
        }
        const update = (width, height) => {
            document.documentElement.style.setProperty(`--${name}-width`, `${width}px`);
            document.documentElement.style.setProperty(`--${name}-height`, `${height}px`);
        };
        const { height, width } = ref.current.getBoundingClientRect();
        update(width, height);
        const ro = new ResizeObserver(entries => {
            for (const entry of entries) {
                update(entry.contentRect.width, entry.contentRect.height);
            }
        });
        ro.observe(ref.current);
        return () => ro.disconnect();
    }, [name, ref]);
}
//# sourceMappingURL=use-css-var-dims.js.map