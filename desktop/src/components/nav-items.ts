/**
 * The window's navigation, in two groups.
 *
 * Copy-adapted from `web/components/operator/nav-items.ts` — same shape (24×24
 * path data, groups with a label, the most-specific-match helper), the desktop
 * app's own pages. The two products navigate the same way on purpose.
 *
 * `Page` is imported as a type only, so this does not create a cycle with
 * App.tsx — the same thing HomePage.tsx already does.
 */

import type { Page } from "@/App";

export type NavItem = { key: Page; label: string; icon: string };
export type NavGroup = { label: string; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Operate",
    items: [
      { key: "home", label: "Home", icon: "M12 3l9 8h-3v9h-5v-6H11v6H6v-9H3l9-8z" },
      // A checklist: the steps from a drone out of the box to a session.
      { key: "setup", label: "Set up", icon: "M4 5h2v2H4V5zm4 0h12v2H8V5zM4 11h2v2H4v-2zm4 0h12v2H8v-2zm-4 6h2v2H4v-2zm4 0h12v2H8v-2z" },
      // The gamepad, taken verbatim from the web sidebar's "Manual" item: the
      // Control page IS the gamepad page, so the shared icon is the right one.
      { key: "control", label: "Control", icon: "M7 9h10a4 4 0 014 4v2a4 4 0 01-4 4H7a4 4 0 01-4-4v-2a4 4 0 014-4zm1 3v2H6v1.5h2V17h1.5v-1.5h2V14h-2v-2H8zm8 1a1 1 0 100 2 1 1 0 000-2z" },
      { key: "sessions", label: "Sessions", icon: "M4 5h16v2H4V5zm0 5h16v2H4v-2zm0 5h10v2H4v-2z" },
      // A play triangle: watch a recorded flight again (pages/playback/).
      { key: "playback", label: "Playback", icon: "M8 5v14l11-7L8 5z" },
    ],
  },
  {
    label: "Live sensors",
    items: [
      // "Flights" from the web sidebar — the same rising trace.
      { key: "flight", label: "Flight", icon: "M3 17l6-6 4 4 8-8v4h2V4h-7v2h4l-7 7-4-4-7 7z" },
      { key: "power", label: "Power", icon: "M7 4h4V2h2v2h4a1 1 0 011 1v16a1 1 0 01-1 1H7a1 1 0 01-1-1V5a1 1 0 011-1zm5 4l-3.2 6.4h2.4l-.7 4.1 3.7-6.5h-2.3L12 8z" },
      { key: "environment", label: "Environment", icon: "M12 2a3 3 0 00-3 3v8.6a5 5 0 106 0V5a3 3 0 00-3-3zm0 2a1 1 0 011 1v9.7l.6.4a3 3 0 11-3.2 0l.6-.4V5a1 1 0 011-1z" },
      { key: "motion", label: "Motion", icon: "M12 10a2 2 0 100 4 2 2 0 000-4zm0-4a6 6 0 016 6h-2a4 4 0 00-4-4V6zm0-4a10 10 0 0110 10h-2a8 8 0 00-8-8V2z" },
      // "Plan" from the web sidebar — a pin, which is what positioning is.
      { key: "positioning", label: "Positioning", icon: "M12 2a7 7 0 00-7 7c0 5 7 13 7 13s7-8 7-13a7 7 0 00-7-7zm0 9.5A2.5 2.5 0 1112 6a2.5 2.5 0 010 5.5z" },
    ],
  },
];
