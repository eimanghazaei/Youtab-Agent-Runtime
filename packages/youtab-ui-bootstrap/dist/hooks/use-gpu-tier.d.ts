/**
 * Tiers:
 *  0 — no WebGL / software renderer / prefers-reduced-motion / WebGL ctx creation failed
 *  1 — low-end GPU (integrated, mobile, or failed perf benchmark)
 *  2 — capable GPU (discrete / high-end integrated)
 *
 * Detection runs **synchroyoutably** the first time this module is evaluated
 * on the client (see the IIFE at the bottom of the file). That means any
 * consumer reading `$gpuTier` during its first render already sees the
 * post-detection value, so WebGL components can avoid trying to create a
 * `THREE.WebGLRenderer` on hardware where context creation will fail.
 *
 * The previous version ran the probe inside `nanostores`'s `onMount`
 * lifecycle, which fires from a microtask after the first listener
 * subscribes — i.e. after the first React commit. By that point overlay
 * components had already executed `new THREE.WebGLRenderer(...)` against
 * the optimistic default, logged `Error creating WebGL context`, and only
 * unmounted on a follow-up render. Eager module-load detection closes that
 * race.
 */
export declare const $gpuTier: import("nanostores").PreinitializedWritableAtom<GpuTier> & object;
export declare function useGpuTier(): GpuTier;
type GpuTier = 0 | 1 | 2;
export {};
