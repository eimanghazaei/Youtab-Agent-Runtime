import { jsx as _jsx } from "react/jsx-runtime";
import { cn } from '../../utils';
export function Socials({ className, items, onNavigate, ...rest }) {
    return (_jsx("div", { className: cn('flex items-center gap-3', className), ...rest, children: items.map(({ external = true, href, icon: Icon, label, onClick }) => (_jsx("a", { className: "opacity-60 transition-opacity hover:opacity-100", href: href, onClick: e => {
                onClick?.(e);
                onNavigate?.();
            }, rel: external ? 'noopener noreferrer' : undefined, target: external ? '_blank' : undefined, title: label, children: _jsx(Icon, {}) }, label))) }));
}
//# sourceMappingURL=socials.js.map