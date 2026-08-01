"use client";
import { jsx } from "react/jsx-runtime";
import * as CheckboxPrimitive from "@radix-ui/react-checkbox";
import { forwardRef } from "react";
import { cn } from "../../utils/index.js";
import { CheckIcon } from "./icons/check.js";
export const Checkbox = forwardRef(function Checkbox2({ className, ...props }, ref) {
  return /* @__PURE__ */ jsx(
    CheckboxPrimitive.Root,
    {
      className: cn(
        "peer flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center border transition-colors outline-none",
        "focus-visible:ring-1 focus-visible:ring-midground/30",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "data-[state=unchecked]:border-midground/20 data-[state=unchecked]:bg-background",
        "data-[state=unchecked]:hover:border-midground/30",
        "data-[state=checked]:border-midground/30 data-[state=checked]:bg-midground/15",
        "data-[state=indeterminate]:border-midground/30 data-[state=indeterminate]:bg-midground/15",
        className
      ),
      ref,
      ...props,
      children: /* @__PURE__ */ jsx(CheckboxPrimitive.Indicator, { className: "flex items-center justify-center text-current", children: /* @__PURE__ */ jsx(CheckIcon, { className: "h-3 w-3 text-midground" }) })
    }
  );
});
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsiPHN0ZGluPiJdLAogICJzb3VyY2VzQ29udGVudCI6IFsiJ3VzZSBjbGllbnQnXG5cbmltcG9ydCAqIGFzIENoZWNrYm94UHJpbWl0aXZlIGZyb20gJ0ByYWRpeC11aS9yZWFjdC1jaGVja2JveCdcbmltcG9ydCB7IGZvcndhcmRSZWYsIHR5cGUgQ29tcG9uZW50UHJvcHNXaXRob3V0UmVmLCB0eXBlIEVsZW1lbnRSZWYgfSBmcm9tICdyZWFjdCdcblxuaW1wb3J0IHsgY24gfSBmcm9tICcuLi8uLi91dGlscydcblxuaW1wb3J0IHsgQ2hlY2tJY29uIH0gZnJvbSAnLi9pY29ucy9jaGVjaydcblxuZXhwb3J0IGNvbnN0IENoZWNrYm94ID0gZm9yd2FyZFJlZjxcbiAgRWxlbWVudFJlZjx0eXBlb2YgQ2hlY2tib3hQcmltaXRpdmUuUm9vdD4sXG4gIENoZWNrYm94UHJvcHNcbj4oZnVuY3Rpb24gQ2hlY2tib3goeyBjbGFzc05hbWUsIC4uLnByb3BzIH0sIHJlZikge1xuICByZXR1cm4gKFxuICAgIDxDaGVja2JveFByaW1pdGl2ZS5Sb290XG4gICAgICBjbGFzc05hbWU9e2NuKFxuICAgICAgICAncGVlciBmbGV4IGgtNCB3LTQgc2hyaW5rLTAgY3Vyc29yLXBvaW50ZXIgaXRlbXMtY2VudGVyIGp1c3RpZnktY2VudGVyIGJvcmRlciB0cmFuc2l0aW9uLWNvbG9ycyBvdXRsaW5lLW5vbmUnLFxuICAgICAgICAnZm9jdXMtdmlzaWJsZTpyaW5nLTEgZm9jdXMtdmlzaWJsZTpyaW5nLW1pZGdyb3VuZC8zMCcsXG4gICAgICAgICdkaXNhYmxlZDpjdXJzb3Itbm90LWFsbG93ZWQgZGlzYWJsZWQ6b3BhY2l0eS01MCcsXG4gICAgICAgICdkYXRhLVtzdGF0ZT11bmNoZWNrZWRdOmJvcmRlci1taWRncm91bmQvMjAgZGF0YS1bc3RhdGU9dW5jaGVja2VkXTpiZy1iYWNrZ3JvdW5kJyxcbiAgICAgICAgJ2RhdGEtW3N0YXRlPXVuY2hlY2tlZF06aG92ZXI6Ym9yZGVyLW1pZGdyb3VuZC8zMCcsXG4gICAgICAgICdkYXRhLVtzdGF0ZT1jaGVja2VkXTpib3JkZXItbWlkZ3JvdW5kLzMwIGRhdGEtW3N0YXRlPWNoZWNrZWRdOmJnLW1pZGdyb3VuZC8xNScsXG4gICAgICAgICdkYXRhLVtzdGF0ZT1pbmRldGVybWluYXRlXTpib3JkZXItbWlkZ3JvdW5kLzMwIGRhdGEtW3N0YXRlPWluZGV0ZXJtaW5hdGVdOmJnLW1pZGdyb3VuZC8xNScsXG4gICAgICAgIGNsYXNzTmFtZVxuICAgICAgKX1cbiAgICAgIHJlZj17cmVmfVxuICAgICAgey4uLnByb3BzfVxuICAgID5cbiAgICAgIDxDaGVja2JveFByaW1pdGl2ZS5JbmRpY2F0b3IgY2xhc3NOYW1lPVwiZmxleCBpdGVtcy1jZW50ZXIganVzdGlmeS1jZW50ZXIgdGV4dC1jdXJyZW50XCI+XG4gICAgICAgIDxDaGVja0ljb24gY2xhc3NOYW1lPVwiaC0zIHctMyB0ZXh0LW1pZGdyb3VuZFwiIC8+XG4gICAgICA8L0NoZWNrYm94UHJpbWl0aXZlLkluZGljYXRvcj5cbiAgICA8L0NoZWNrYm94UHJpbWl0aXZlLlJvb3Q+XG4gIClcbn0pXG5cbnR5cGUgQ2hlY2tib3hQcm9wcyA9IENvbXBvbmVudFByb3BzV2l0aG91dFJlZjx0eXBlb2YgQ2hlY2tib3hQcmltaXRpdmUuUm9vdD5cbiJdLAogICJtYXBwaW5ncyI6ICI7QUE2QlE7QUEzQlIsWUFBWSx1QkFBdUI7QUFDbkMsU0FBUyxrQkFBa0U7QUFFM0UsU0FBUyxVQUFVO0FBRW5CLFNBQVMsaUJBQWlCO0FBRW5CLGFBQU0sV0FBVyxXQUd0QixTQUFTQSxVQUFTLEVBQUUsV0FBVyxHQUFHLE1BQU0sR0FBRyxLQUFLO0FBQ2hELFNBQ0U7QUFBQSxJQUFDLGtCQUFrQjtBQUFBLElBQWxCO0FBQUEsTUFDQyxXQUFXO0FBQUEsUUFDVDtBQUFBLFFBQ0E7QUFBQSxRQUNBO0FBQUEsUUFDQTtBQUFBLFFBQ0E7QUFBQSxRQUNBO0FBQUEsUUFDQTtBQUFBLFFBQ0E7QUFBQSxNQUNGO0FBQUEsTUFDQTtBQUFBLE1BQ0MsR0FBRztBQUFBLE1BRUosOEJBQUMsa0JBQWtCLFdBQWxCLEVBQTRCLFdBQVUsaURBQ3JDLDhCQUFDLGFBQVUsV0FBVSwwQkFBeUIsR0FDaEQ7QUFBQTtBQUFBLEVBQ0Y7QUFFSixDQUFDOyIsCiAgIm5hbWVzIjogWyJDaGVja2JveCJdCn0K
