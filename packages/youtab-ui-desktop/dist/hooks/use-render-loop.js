'use client';
export function runRenderLoop({ el, minIntervalMs = 0, onFrame }) {
    let running = true;
    let visible = !document.hidden;
    let inView = true;
    let last = performance.now();
    let raf = 0;
    let timer;
    const onVisibility = () => {
        visible = !document.hidden;
        // When we come back from a hidden tab, reset the clock so the next
        // frame's delta is ~one frame, not "hours since I was hidden".
        if (visible) {
            last = performance.now();
            schedule();
        }
    };
    const io = new IntersectionObserver(entries => {
        const wasInView = inView;
        inView = entries.some(e => e.isIntersecting);
        if (!wasInView && inView) {
            last = performance.now();
            schedule();
        }
    }, { threshold: 0 });
    io.observe(el);
    document.addEventListener('visibilitychange', onVisibility);
    const tick = () => {
        if (!running)
            return;
        if (!visible || !inView) {
            // Don't reschedule — we'll be re-kicked by visibilitychange or IO.
            return;
        }
        const now = performance.now();
        const delta = (now - last) / 1000;
        last = now;
        onFrame(delta);
        schedule();
    };
    function schedule() {
        if (!running || !visible || !inView)
            return;
        if (minIntervalMs > 0) {
            timer = setTimeout(tick, minIntervalMs);
        }
        else {
            raf = requestAnimationFrame(tick);
        }
    }
    schedule();
    return () => {
        running = false;
        io.disconnect();
        document.removeEventListener('visibilitychange', onVisibility);
        cancelAnimationFrame(raf);
        if (timer !== undefined) {
            clearTimeout(timer);
        }
    };
}
//# sourceMappingURL=use-render-loop.js.map