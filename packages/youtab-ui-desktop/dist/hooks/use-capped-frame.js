'use client';
import { useFrame, useThree } from '@react-three/fiber';
import { useRef } from 'react';
export function useCappedFrame(cb, max) {
    const last = useRef(performance.now());
    const { size } = useThree();
    const interval = 1e3 / (max ?? (size.width < 1024 ? 60 : 120));
    useFrame((st, delta) => {
        if (performance.now() - last.current > interval) {
            last.current = performance.now();
            cb(st, delta);
        }
    });
}
//# sourceMappingURL=use-capped-frame.js.map