---
version: alpha
name: "Extend AI — Document Processing Infrastructure"
description: "Extend's design system is built on a strict monochrome foundation. pure black (#000000) and pure white (#ffffff). that inverts cleanly between light and dark themes. The 404 error page reveals the core typographic and spatial DNA: a custom font stack (systemia, dmMono, abcFavorit, dwight, mindway) layered over system-ui fallbacks, with a centered flex layout, a vertical divider separator, and no decorative shadows or gradients. The footer strip uses a warm off-white (#e5e5e3-ish) surface. Typography is tightly controlled with tracked mono labels (dmMono at 0.78px letter-spacing) alongside proportional body text. The overall aesthetic is infrastructure-grade: precise, minimal ornamentation, and built for legibility at scale."
colors:
  footer-mist: "#e5e5e3"
  void-black: "#000000"
  ghost-white: "#ffffff"
  surface-white: "#ffffff"
  ink-black: "#000000"
typography:
  error-code:
    fontFamily: "system-ui"
    fontSize: "24px"
    fontWeight: "500"
    lineHeight: "49px"
  error-message:
    fontFamily: "system-ui"
    fontSize: "14px"
    fontWeight: "400"
    lineHeight: "49px"
  body-base:
    fontFamily: "systemia"
    fontSize: "16px"
    fontWeight: "400"
    lineHeight: "24px"
  mono-label:
    fontFamily: "dmMono"
    fontSize: "13px"
    fontWeight: "400"
    letterSpacing: "0.78px"
  body-small:
    fontFamily: "systemia"
    fontSize: "15px"
    fontWeight: "400"
    lineHeight: "24.375px"
rounded:
  sm: "calc(0.625rem - 4px)"
  md: "calc(0.625rem - 2px)"
  lg: "0.625rem"
  none: "0px"
spacing:
  xs: "6px"
  sm: "10px"
  md: "12px"
  base: "16px"
  lg: "20px"
  xl: "23px"
  2xl: "32px"
  container-height: "71.1px"
components:
  error-state:
    layout: "flex row, centered horizontally and vertically in viewport"
    errorCodeFontSize: "24px"
    errorCodeFontWeight: "500"
    errorCodeFontFamily: "system-ui"
    errorCodeColorDark: "{colors.surface-white}"
    errorCodeColorLight: "{colors.ink-black}"
    divider: "1.11px right border, rgba(255,255,255,0.3) in dark / lab(90.952) in light"
    dividerPaddingRight: "{spacing.xl}"
    messageFontSize: "14px"
    messageFontWeight: "400"
    messageFontFamily: "system-ui"
    rounded: "{rounded.none}"
    boxShadow: "none"
  footer:
    backgroundColor: "#e5e5e3 (approx)"
    height: "~71px (71.1px spacing token)"
    width: "100vw"
    rounded: "{rounded.none}"
    boxShadow: "none"
  label:
    fontFamily: "dmMono"
    fontSize: "13px"
    fontWeight: "400"
    lineHeight: "normal"
    letterSpacing: "0.78px"
    colorLight: "{colors.ink-black}"
    colorDark: "{colors.surface-white}"
---

## Overview

Extend's design system is built on a strict monochrome foundation. pure black (#000000) and pure white (#ffffff). that inverts cleanly between light and dark themes. The 404 error page reveals the core typographic and spatial DNA: a custom font stack (systemia, dmMono, abcFavorit, dwight, mindway) layered over system-ui fallbacks, with a centered flex layout, a vertical divider separator, and no decorative shadows or gradients. The footer strip uses a warm off-white (#e5e5e3-ish) surface. Typography is tightly controlled with tracked mono labels (dmMono at 0.78px letter-spacing) alongside proportional body text. The overall aesthetic is infrastructure-grade: precise, minimal ornamentation, and built for legibility at scale.

**Signature traits:**
- Dual typeface system: Pairs system-ui and systemia across the type hierarchy.
- Monochrome palette: Relies entirely on neutral tones with no chromatic accent.

## Colors

The palette uses 6 validated color tokens across 2 theme profiles. Semantic roles stay attached to observed usage so generation agents can choose accents without inventing new color meaning.

**Semantic naming:**
- **surface-background** maps to `surface-white`: Role "background" is grounded by usage context "Full-page background in light theme; header surface zone".
- **content-text** maps to `ink-black`: Role "text" is grounded by usage context "Primary text color for headings and body in light theme".

### Dark Theme

### Text Scale
- **Ghost White** (#ffffff): Primary text color for headings and body in dark theme. Role: text. {authored: rgb(255, 255, 255), space: rgb}

### Surface & Shadows
- **Footer Mist** (#e5e5e3): Footer strip background, persists across both themes as a warm neutral surface. Role: background.
- **Void Black** (#000000): Full-page background in dark theme. Role: background. {authored: rgb(0, 0, 0), space: rgb}

### Light Theme

### Text Scale
- **Ink Black** (#000000): Primary text color for headings and body in light theme. Role: text. {authored: rgb(0, 0, 0), space: rgb}

### Surface & Shadows
- **Footer Mist** (#e5e5e3): Footer strip background, warm off-white surface visible in both themes. Role: background.
- **Surface White** (#ffffff): Full-page background in light theme; header surface zone. Role: background. {authored: rgb(255, 255, 255), space: rgb}

## Typography

Typography uses system-ui, systemia, dmMono across extracted hierarchy roles. Keep hierarchy mapped to these token rows before adding decorative type styles.

Mixes system-ui and systemia and dmMono for visual contrast. Weight range spans medium, regular. Sizes range from 13px to 24px.

### Font Roles
- **Headline Font**: systemia
- **Body Font**: systemia

### Type Scale Evidence
| Role | Font | Size | Weight | Line Height | Letter Spacing | Stack / Features | Notes |
|------|------|------|--------|-------------|----------------|------------------|-------|
| Large numeric error code display (404), prominent heading weight | system-ui | 24px | 500 | 49px | normal | system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif, Apple Color Emoji, Segoe UI Emoji | Extracted token |
| Inline error description text beside the error code | system-ui | 14px | 400 | 49px | normal | system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif, Apple Color Emoji, Segoe UI Emoji | Extracted token |
| Primary body text across the interface | systemia | 16px | 400 | 24px | normal | systemia, systemia Fallback | Extracted token |
| Monospaced labels, tags, code snippets, and metadata annotations | dmMono | 13px | 400 | normal | 0.78px | dmMono, dmMono Fallback | Extracted token |
| Secondary body text, slightly reduced size variant | systemia | 15px | 400 | 24.375px | normal | systemia, systemia Fallback | Extracted token |

## Layout

Responsive system uses 3 breakpoint tier(s): mobile, tablet, desktop.

This system uses a 4px base grid with scale values 4, 6, 8, 10, 12, 16, 20, 23, 24, 32, 64.

### Responsive Strategy
- **mobile (0-1023.98px)**: Constrain layout for small viewports and prioritize vertical stacking.
- **tablet (768-1023.98px)**: Increase spacing and column structure for medium-width viewports.
- **desktop (>= 1024px)**: Expand layout density and horizontal composition for wide viewports.

### Spacing System
| Token | Value | Px | Notes |
|------|-------|----|-------|
| xs | 6px | 6 | Extracted spacing token |
| sm | 10px | 10 | Extracted spacing token |
| md | 12px | 12 | Extracted spacing token |
| base | 16px | 16 | Extracted spacing token |
| lg | 20px | 20 | Extracted spacing token |
| xl | 23px | 23 | Extracted spacing token |
| 2xl | 32px | 32 | Extracted spacing token |
| container-height | 71.1px | 71.1 | Extracted spacing token |

## Elevation & Depth

Keep depth flat unless validated shadow or interaction evidence appears in the extraction payload. Do not invent shadows beyond this evidence boundary.

### Shadow Evidence
| Shadow Token | Layers | Details |
|--------------|--------|---------|
| n/a | 0 | No validated shadow payload |

### Interaction Signals
| Theme | Signal | Evidence |
|-------|--------|----------|
| Light | outline-color | oklab(0.707999 -0.00000712276 0.0000166297 / 0.5) |
| Light | outline-width | 3.33333px |
| Light | outline-offset | 0px |
| Dark | outline-color | oklab(0.707999 -0.00000712276 0.0000166297 / 0.5) |
| Dark | outline-width | 3.33333px |
| Dark | outline-offset | 0px |

## Shapes

Shape language maps directly to rounded tokens. Keep component corners consistent with the role mapping below before introducing bespoke geometry.

### Radius Roles
| Token | Value | Px | Role Mapping |
|------|-------|----|--------------|
| none | 0px | 0 | Hairline corner |
| sm | calc(0.625rem - 4px) | 6 | Subtle corner |
| md | calc(0.625rem - 2px) | 8 | Control corner |
| lg | 0.625rem | 10 | Control corner |

### Geometry Evidence
| Radius Token | Shape | Units |
|--------------|-------|-------|
| sm | calc(0.625rem - 4px) | calc |
| md | calc(0.625rem - 2px) | calc |
| lg | 0.625rem | rem |
| none | 0px | px |

## Components

Components should be recreated from token references first, then tuned with variant notes and probe-backed state guidance.
- **404 Error Block**: Centered full-viewport error display with a large numeric code, a vertical right-border divider, and an inline message. No background fill, no shadow, no border-radius.
- **Footer Strip**: Full-width footer band with a warm off-white/light-gray background (#e5e5e3 approx), visible in both light and dark themes as a contrasting base strip.
- **Mono Tag / Label**: Small monospaced text element using dmMono at 13px with 0.78px letter-spacing, used for metadata, tags, or code-adjacent labels.

### Error State

**Default**
- layout: flex row, centered horizontally and vertically in viewport
- errorCodeFontSize: 24px
- errorCodeFontWeight: 500
- errorCodeFontFamily: system-ui
- errorCodeColorDark: #ffffff
- errorCodeColorLight: #000000
- divider: 1.11px right border, rgba(255,255,255,0.3) in dark / lab(90.952) in light
- dividerPaddingRight: 23px
- messageFontSize: 14px
- messageFontWeight: 400
- messageFontFamily: system-ui
- rounded: 0px
- boxShadow: none
- State guidance: Probe-backed: h1.next-error-h1 has border-right 1.11px, padding-right 23px. h2 has font-size 14px. Both confirmed via computed styles.

### Footer

**Default**
- backgroundColor: #e5e5e3 (approx)
- height: ~71px (71.1px spacing token)
- width: 100vw
- rounded: 0px
- boxShadow: none
- State guidance: Visually confirmed in both screenshots. Exact hex approximated from screenshot; not in CSSOM frequency data — treat as medium confidence.

### Label

**Default**
- fontFamily: dmMono
- fontSize: 13px
- fontWeight: 400
- lineHeight: normal
- letterSpacing: 0.78px
- colorLight: #000000
- colorDark: #ffffff
- State guidance: Confirmed via CSSOM frequency: dmMono 13px w400 / normal ls:0.78px ×4 hits.

## Do's and Don'ts

Guardrails protect Dual typeface system, Monochrome palette without adding unsupported visual claims.

| Do | Don't |
|----|---------|
| Do maintain consistent spacing using the base grid | Don't make unsupported claims about absent visual features |
| Do maintain WCAG AA contrast ratios (4.5:1 for normal text) | Don't mix rounded and sharp corners in the same view |
| Do use the primary color only for the single most important action per screen |  |
| Do verify evidence before writing new design-system guidance |  |

## Responsive Evidence

### Breakpoints
| Name | Width | Key Changes |
|------|-------|-------------|
| Breakpoint 1 | <= 767.98px | not all and (max-width: 767.98px) |
| Breakpoint 2 | <= 1023.98px | not all and (max-width: 1023.98px) |
| Mobile | 0-767.98px | not all and (min-width: 0px) and (max-width: 767.98px) |
| Mobile | 0-1023.98px | not all and (min-width: 0px) and (max-width: 1023.98px) |
| Mobile | >= 0px | not all and (min-width: 0px) |
| Tablet | 768-1023.98px | not all and (min-width: 768px) and (max-width: 1023.98px) |
| Tablet | >= 768px | not all and (min-width: 768px) |
| Desktop | >= 1024px | not all and (min-width: 1024px) |
| Breakpoint 9 | Unknown | (prefers-color-scheme: dark) |

## Agent Prompt Guide

### Example Component Prompts
- Create 404 Error Block variant that preserves Centered full-viewport error display with a large numeric code, a vertical right-border divider, and an inline message. No background fill, no shadow, no border-radius..
- Create Footer Strip variant that preserves Full-width footer band with a warm off-white/light-gray background (#e5e5e3 approx), visible in both light and dark themes as a contrasting base strip..
- Create Mono Tag / Label variant that preserves Small monospaced text element using dmMono at 13px with 0.78px letter-spacing, used for metadata, tags, or code-adjacent labels..

### Iteration Guide
1. Start with extracted palette and typography roles only.
2. Map spacing and radius directly from token tables before visual polish.
3. Apply component patterns one section at a time and compare against source intent.
4. Keep elevation claims tied to explicit evidence in output.
5. Iterate with smallest diffs and re-check section hierarchy after each change.
