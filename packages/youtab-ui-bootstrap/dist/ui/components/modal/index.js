"use client";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "../../../utils/index.js";
export function Modal({
  children,
  className,
  id,
  trigger,
  ...props
}) {
  const ref = useRef(null);
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const open = useCallback(() => ref.current?.showModal(), []);
  const close = useCallback(() => ref.current?.close(), []);
  return /* @__PURE__ */ jsxs(Fragment, { children: [
    trigger({ close, open }),
    mounted && createPortal(
      /* @__PURE__ */ jsx(
        "dialog",
        {
          className: cn("modal", className),
          onClick: (e) => e.target === e.currentTarget && close(),
          ...{ id, ref },
          ...props,
          children: /* @__PURE__ */ jsx("div", { className: "modal-body post", children })
        }
      ),
      document.body
    )
  ] });
}
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsiPHN0ZGluPiJdLAogICJzb3VyY2VzQ29udGVudCI6IFsiJ3VzZSBjbGllbnQnXG5cbmltcG9ydCB7IHVzZUNhbGxiYWNrLCB1c2VFZmZlY3QsIHVzZVJlZiwgdXNlU3RhdGUgfSBmcm9tICdyZWFjdCdcbmltcG9ydCB7IGNyZWF0ZVBvcnRhbCB9IGZyb20gJ3JlYWN0LWRvbSdcblxuaW1wb3J0IHsgY24gfSBmcm9tICcuLi8uLi8uLi91dGlscydcblxuZXhwb3J0IGZ1bmN0aW9uIE1vZGFsKHtcbiAgY2hpbGRyZW4sXG4gIGNsYXNzTmFtZSxcbiAgaWQsXG4gIHRyaWdnZXIsXG4gIC4uLnByb3BzXG59OiBNb2RhbFByb3BzKSB7XG4gIGNvbnN0IHJlZiA9IHVzZVJlZjxIVE1MRGlhbG9nRWxlbWVudD4obnVsbClcbiAgY29uc3QgW21vdW50ZWQsIHNldE1vdW50ZWRdID0gdXNlU3RhdGUoZmFsc2UpXG5cbiAgdXNlRWZmZWN0KCgpID0+IHNldE1vdW50ZWQodHJ1ZSksIFtdKVxuXG4gIGNvbnN0IG9wZW4gPSB1c2VDYWxsYmFjaygoKSA9PiByZWYuY3VycmVudD8uc2hvd01vZGFsKCksIFtdKVxuICBjb25zdCBjbG9zZSA9IHVzZUNhbGxiYWNrKCgpID0+IHJlZi5jdXJyZW50Py5jbG9zZSgpLCBbXSlcblxuICByZXR1cm4gKFxuICAgIDw+XG4gICAgICB7dHJpZ2dlcih7IGNsb3NlLCBvcGVuIH0pfVxuXG4gICAgICB7bW91bnRlZCAmJlxuICAgICAgICBjcmVhdGVQb3J0YWwoXG4gICAgICAgICAgPGRpYWxvZ1xuICAgICAgICAgICAgY2xhc3NOYW1lPXtjbignbW9kYWwnLCBjbGFzc05hbWUpfVxuICAgICAgICAgICAgb25DbGljaz17ZSA9PiBlLnRhcmdldCA9PT0gZS5jdXJyZW50VGFyZ2V0ICYmIGNsb3NlKCl9XG4gICAgICAgICAgICB7Li4ueyBpZCwgcmVmIH19XG4gICAgICAgICAgICB7Li4ucHJvcHN9XG4gICAgICAgICAgPlxuICAgICAgICAgICAgPGRpdiBjbGFzc05hbWU9XCJtb2RhbC1ib2R5IHBvc3RcIj57Y2hpbGRyZW59PC9kaXY+XG4gICAgICAgICAgPC9kaWFsb2c+LFxuICAgICAgICAgIGRvY3VtZW50LmJvZHlcbiAgICAgICAgKX1cbiAgICA8Lz5cbiAgKVxufVxuXG5pbnRlcmZhY2UgTW9kYWxQcm9wcyBleHRlbmRzIE9taXQ8UmVhY3QuQ29tcG9uZW50UHJvcHM8J2RpYWxvZyc+LCAnb3Blbic+IHtcbiAgdHJpZ2dlcjogKGNvbnRyb2xzOiB7XG4gICAgY2xvc2U6ICgpID0+IHZvaWRcbiAgICBvcGVuOiAoKSA9PiB2b2lkXG4gIH0pID0+IFJlYWN0LlJlYWN0Tm9kZVxufVxuIl0sCiAgIm1hcHBpbmdzIjogIjtBQXVCSSxtQkFXUSxLQVhSO0FBckJKLFNBQVMsYUFBYSxXQUFXLFFBQVEsZ0JBQWdCO0FBQ3pELFNBQVMsb0JBQW9CO0FBRTdCLFNBQVMsVUFBVTtBQUVaLGdCQUFTLE1BQU07QUFBQSxFQUNwQjtBQUFBLEVBQ0E7QUFBQSxFQUNBO0FBQUEsRUFDQTtBQUFBLEVBQ0EsR0FBRztBQUNMLEdBQWU7QUFDYixRQUFNLE1BQU0sT0FBMEIsSUFBSTtBQUMxQyxRQUFNLENBQUMsU0FBUyxVQUFVLElBQUksU0FBUyxLQUFLO0FBRTVDLFlBQVUsTUFBTSxXQUFXLElBQUksR0FBRyxDQUFDLENBQUM7QUFFcEMsUUFBTSxPQUFPLFlBQVksTUFBTSxJQUFJLFNBQVMsVUFBVSxHQUFHLENBQUMsQ0FBQztBQUMzRCxRQUFNLFFBQVEsWUFBWSxNQUFNLElBQUksU0FBUyxNQUFNLEdBQUcsQ0FBQyxDQUFDO0FBRXhELFNBQ0UsaUNBQ0c7QUFBQSxZQUFRLEVBQUUsT0FBTyxLQUFLLENBQUM7QUFBQSxJQUV2QixXQUNDO0FBQUEsTUFDRTtBQUFBLFFBQUM7QUFBQTtBQUFBLFVBQ0MsV0FBVyxHQUFHLFNBQVMsU0FBUztBQUFBLFVBQ2hDLFNBQVMsT0FBSyxFQUFFLFdBQVcsRUFBRSxpQkFBaUIsTUFBTTtBQUFBLFVBQ25ELEdBQUcsRUFBRSxJQUFJLElBQUk7QUFBQSxVQUNiLEdBQUc7QUFBQSxVQUVKLDhCQUFDLFNBQUksV0FBVSxtQkFBbUIsVUFBUztBQUFBO0FBQUEsTUFDN0M7QUFBQSxNQUNBLFNBQVM7QUFBQSxJQUNYO0FBQUEsS0FDSjtBQUVKOyIsCiAgIm5hbWVzIjogW10KfQo=
