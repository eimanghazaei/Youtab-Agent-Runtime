'use client';
import { jsxs as _jsxs, jsx as _jsx } from "react/jsx-runtime";
import { useRef } from 'react';
import { useCssVarDims } from '../hooks/use-css-var-dims';
import { Cell, Grid } from './components/grid';
import { Socials } from './components/socials';
import { ThemeToggle } from './components/theme-toggle';
import { Small } from './components/typography/small';
const DEFAULT_GROUPS = [
    { label: 'Product', links: ['Overview', 'Features', 'Pricing'] },
    { label: 'Resources', links: ['Docs', 'Blog', 'Support'] },
    { label: 'Company', links: ['About', 'Careers', 'Contact'] },
    { label: 'Legal', links: ['Privacy', 'Terms', 'License'] }
];
export function Footer({ className, groups = DEFAULT_GROUPS, LinkComponent = 'a', socials, socialsLabel = 'Socials', style, themeLabel = 'Theme', themeToggle = false }) {
    const ref = useRef(null);
    useCssVarDims('footer', ref);
    const hasSocials = (socials?.length ?? 0) > 0;
    const hasChrome = hasSocials || themeToggle;
    return (_jsxs("footer", { className: className, ref: ref, style: style, children: [_jsxs(Grid, { children: [_jsx(Cell, { children: _jsxs(Small, { className: "opacity-50", children: ["\u00A9", new Date().getFullYear()] }) }), groups.map(({ label, links }) => (_jsxs(Cell, { children: [_jsx(Small, { className: "opacity-50", children: label }), _jsx("nav", { className: "mt-3 flex flex-col gap-2", children: links.map(link => {
                                    const href = typeof link === 'string'
                                        ? `/${link.toLowerCase()}`
                                        : link.href;
                                    const label = typeof link === 'string' ? link : link.label;
                                    return (_jsx(Small, { as: LinkComponent, className: "underline", href: href, children: label }, label));
                                }) })] }, label)))] }), hasChrome && (_jsxs(Grid, { children: [hasSocials && (_jsxs(Cell, { className: "flex items-start justify-between", children: [_jsx(Small, { className: "opacity-50", children: socialsLabel }), _jsx(Socials, { items: socials })] })), themeToggle && (_jsxs(Cell, { className: "flex items-start justify-between", children: [_jsx(Small, { className: "opacity-50", children: themeLabel }), _jsx(ThemeToggle, {})] }))] }))] }));
}
//# sourceMappingURL=footer.js.map