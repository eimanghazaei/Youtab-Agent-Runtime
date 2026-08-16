'use client';
import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { AnimatePresence, motion } from 'motion/react';
import { createElement, useCallback, useRef, useState } from 'react';
import { useCssVarDims } from '../hooks/use-css-var-dims';
import { useGpuTier } from '../hooks/use-gpu-tier';
import { cn } from '../utils';
import { Blink } from './components/blink';
import { Cell, Grid } from './components/grid';
import { HoverBg } from './components/hover-bg';
import { HamburgerIcon } from './components/icons/hamburger';
import { Scramble } from './components/scramble';
import { Socials } from './components/socials';
import { ThemeToggle } from './components/theme-toggle';
import { H2 } from './components/typography/h2';
import { Small } from './components/typography/small';
const DEFAULT_BRAND = (_jsxs("hgroup", { className: "flex flex-col gap-2", children: [_jsx(Small, { children: "Youtab" }), _jsx(H2, { children: "Research" })] }));
const DEFAULT_LINKS = [
    { href: '/projects', label: 'Projects' },
    { href: '/participants', label: 'Participants' },
    { href: '/provenance', label: 'Provenance' },
    { href: '/contribute', label: 'Contribute' }
];
export function Header({ brand = DEFAULT_BRAND, brandHref = '/', className, desktopGridStyle, links = DEFAULT_LINKS, LinkComponent = 'a', scramble: scrambleProp = true, socials, socialsLabel = 'Socials', style, themeLabel = 'Theme', themeToggle = false }) {
    const ref = useRef(null);
    useCssVarDims('header', ref);
    // Skip the hover-Scramble rAF loop on tier-0 devices (no GPU / software
    // renderer / `prefers-reduced-motion: reduce`) regardless of the prop.
    const gpuTier = useGpuTier();
    const scramble = scrambleProp && gpuTier > 0;
    const [open, setOpen] = useState(false);
    const close = useCallback(() => setOpen(false), []);
    const hasSocials = (socials?.length ?? 0) > 0;
    const hasMobileChrome = themeToggle || hasSocials;
    return (_jsxs("header", { className: className, ref: ref, style: style, children: [_jsxs(Grid, { className: "hidden border-t border-b lg:grid", style: desktopGridStyle, children: [_jsx(BrandCell, { brand: brand, href: brandHref, LinkComponent: LinkComponent }), links.map(link => (_jsx(NavCell, { link: link, LinkComponent: LinkComponent, scramble: scramble }, link.href))), hasSocials && (_jsxs(Cell, { className: "flex items-start justify-between", children: [_jsx(Small, { className: "opacity-50", children: socialsLabel }), _jsx(Socials, { items: socials })] })), themeToggle && (_jsxs(Cell, { className: "flex items-start justify-between", children: [_jsx(Small, { className: "opacity-50", children: themeLabel }), _jsx(ThemeToggle, {})] }))] }), _jsxs("div", { className: cn('flex items-center justify-between border border-current/20 p-4', 'lg:hidden'), children: [_jsx(BrandLink, { brand: brand, href: brandHref, LinkComponent: LinkComponent }), _jsxs("div", { className: "flex items-center gap-3", children: [themeToggle && _jsx(ThemeToggle, {}), _jsx("button", { "aria-label": open ? 'Close menu' : 'Open menu', className: "relative z-50 cursor-pointer bg-transparent p-2", onClick: () => setOpen(v => !v), type: "button", children: _jsx(HamburgerIcon, { open: open }) })] })] }), _jsx(AnimatePresence, { children: open && (_jsx(motion.div, { animate: { opacity: 1 }, className: cn('bg-background/95 fixed inset-0 z-50 flex flex-col backdrop-blur-sm', 'p-8 lg:hidden'), exit: { opacity: 0 }, initial: { opacity: 0 }, transition: { duration: 0.2 }, children: _jsxs("div", { className: "flex flex-col border border-current/20", children: [_jsxs("div", { className: "flex items-center justify-between border-b border-current/20 p-4", children: [_jsx(BrandLink, { brand: brand, href: brandHref, LinkComponent: LinkComponent, onClick: close }), _jsx("button", { "aria-label": "Close menu", className: "cursor-pointer bg-transparent p-2", onClick: close, type: "button", children: _jsx(HamburgerIcon, { open: true }) })] }), links.map(link => (_jsx(MobileNavLink, { link: link, LinkComponent: LinkComponent, onNavigate: close, scramble: scramble }, link.href))), hasMobileChrome && (_jsxs("div", { className: "flex items-center gap-3 border-b border-current/20 p-4", children: [hasSocials && (_jsxs(_Fragment, { children: [_jsx(Small, { className: "opacity-50", children: socialsLabel }), _jsx(Socials, { items: socials, onNavigate: close })] })), themeToggle && hasSocials && _jsx("span", { className: "flex-1" }), themeToggle && (_jsxs(_Fragment, { children: [_jsx(Small, { className: "opacity-50", children: themeLabel }), _jsx(ThemeToggle, {})] }))] }))] }) })) })] }));
}
function BrandCell({ brand, href, LinkComponent }) {
    return isExternal(href) ? (_jsx(Cell, { href: href, ...EXTERNAL_REL, as: "a", children: brand })) : (_jsx(Cell, { as: LinkComponent, href: href, children: brand }));
}
function BrandLink({ brand, href, LinkComponent, onClick }) {
    if (isExternal(href)) {
        return (_jsx("a", { href: href, onClick: onClick, ...EXTERNAL_REL, children: brand }));
    }
    return createElement(LinkComponent, { href, onClick }, brand);
}
function NavCell({ link, LinkComponent, scramble }) {
    const ref = useRef(null);
    const isExt = link.external ?? isExternal(link.href);
    const inner = (_jsxs(_Fragment, { children: [_jsxs(Small, { children: [scramble ? (_jsx(Scramble, { target: ref, children: link.label })) : (link.label), _jsx(Blink, {})] }), _jsx(HoverBg, {})] }));
    if (isExt) {
        return (_jsx(Cell, { as: "a", className: "group relative cursor-pointer", href: link.href, onClick: link.onClick, ref: ref, ...EXTERNAL_REL, children: inner }));
    }
    return (_jsx(Cell, { as: LinkComponent, className: "group relative cursor-pointer", href: link.href, onClick: link.onClick, ref: ref, children: inner }));
}
function MobileNavLink({ link, LinkComponent, onNavigate, scramble }) {
    const ref = useRef(null);
    const isExt = link.external ?? isExternal(link.href);
    const className = cn('group relative flex cursor-pointer items-center border-b border-current/20 p-4');
    const onClick = (e) => {
        link.onClick?.(e);
        onNavigate();
    };
    const children = (_jsxs(_Fragment, { children: [_jsxs(Small, { children: [scramble ? (_jsx(Scramble, { target: ref, children: link.label })) : (link.label), _jsx(Blink, {})] }), _jsx(HoverBg, {})] }));
    if (isExt) {
        return (_jsx("a", { className: className, href: link.href, onClick: onClick, ref: ref, ...EXTERNAL_REL, children: children }));
    }
    return createElement(LinkComponent, { className, href: link.href, onClick, ref }, children);
}
const EXTERNAL_REL = {
    rel: 'noopener noreferrer',
    target: '_blank'
};
const isExternal = (href) => /^(https?:|mailto:|tel:)/i.test(href);
//# sourceMappingURL=header.js.map