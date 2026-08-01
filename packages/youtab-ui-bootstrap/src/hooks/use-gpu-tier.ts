'use client'

import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'

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
export const $gpuTier = atom<GpuTier>(2)

const SOFTWARE_PATTERNS =
  /swiftshader|llvmpipe|softpipe|software|microsoft basic/i

const LOW_END_PATTERNS =
  /intel.*hd|intel.*uhd|intel.*iris|mali|adreno\s?[1-5]|powervr|apple gpu/i

let detected = false

function detectGpuTier() {
  if (detected || typeof window === 'undefined') {
    return
  }

  detected = true

  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    $gpuTier.set(0)

    return
  }

  let gl: null | WebGLRenderingContext = null

  try {
    const canvas = document.createElement('canvas')
    gl = (canvas.getContext('webgl') ||
      canvas.getContext('experimental-webgl')) as null | WebGLRenderingContext
  } catch {
    // Some sandboxed / hardened contexts throw on getContext rather than
    // returning null (e.g. certain corporate browser policies). Treat as
    // "no WebGL available".
    $gpuTier.set(0)

    return
  }

  if (!gl) {
    $gpuTier.set(0)

    return
  }

  const ext = gl.getExtension('WEBGL_debug_renderer_info')
  const renderer = String(
    ext
      ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)
      : gl.getParameter(gl.RENDERER)
  )

  if (SOFTWARE_PATTERNS.test(renderer)) {
    $gpuTier.set(0)
    gl.getExtension('WEBGL_lose_context')?.loseContext()

    return
  }

  if (LOW_END_PATTERNS.test(renderer)) {
    $gpuTier.set(1)
    gl.getExtension('WEBGL_lose_context')?.loseContext()

    return
  }

  $gpuTier.set(2)

  runBenchmark(gl)
    .then(fps => $gpuTier.set(fps < 30 ? 1 : 2))
    .catch(() => $gpuTier.set(1))
    .finally(() => gl?.getExtension('WEBGL_lose_context')?.loseContext())
}

if (typeof window !== 'undefined') {
  detectGpuTier()
}

function runBenchmark(gl: WebGLRenderingContext): Promise<number> {
  return new Promise(resolve => {
    const vs = gl.createShader(gl.VERTEX_SHADER)!
    const fs = gl.createShader(gl.FRAGMENT_SHADER)!
    gl.shaderSource(
      vs,
      'attribute vec2 a;void main(){gl_Position=vec4(a,0,1);}'
    )
    gl.shaderSource(
      fs,
      'precision highp float;uniform float t;void main(){float v=0.;for(int i=0;i<64;i++)v+=sin(float(i)*t*.01);gl_FragColor=vec4(v*.001);}'
    )
    gl.compileShader(vs)
    gl.compileShader(fs)

    const prog = gl.createProgram()!
    gl.attachShader(prog, vs)
    gl.attachShader(prog, fs)
    gl.linkProgram(prog)
    gl.useProgram(prog)

    const buf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
      gl.STATIC_DRAW
    )
    const a = gl.getAttribLocation(prog, 'a')
    gl.enableVertexAttribArray(a)
    gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0)

    const uT = gl.getUniformLocation(prog, 't')
    let frames = 0
    const start = performance.now()

    const tick = () => {
      gl.uniform1f(uT, frames)
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
      gl.finish()
      frames++

      if (performance.now() - start < 200) {
        requestAnimationFrame(tick)
      } else {
        const elapsed = performance.now() - start
        gl.deleteProgram(prog)
        gl.deleteShader(vs)
        gl.deleteShader(fs)
        gl.deleteBuffer(buf)
        resolve((frames / elapsed) * 1000)
      }
    }

    requestAnimationFrame(tick)
  })
}

export function useGpuTier() {
  return useStore($gpuTier)
}

type GpuTier = 0 | 1 | 2
