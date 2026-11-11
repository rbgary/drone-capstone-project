import { openUrl } from "@tauri-apps/plugin-opener";

/**
 * The dashboard, in a browser.
 *
 * This laptop holds the whole flight history for the sessions it ran, but the
 * dashboard holds every flight from every laptop, with charts and comparisons
 * this window does not try to repeat. Compiled in beside the Supabase project
 * (src-tauri/src/lib.rs) and overridable for a preview deployment.
 */
export const WEB_APP_URL = "https://drone-capstone-project.vercel.app";

export const dashboardUrl = (path = "/app") => `${WEB_APP_URL}${path}`;

/** Opens in the operator's own browser, never inside this window. */
export async function openDashboard(path = "/app"): Promise<void> {
  await openUrl(dashboardUrl(path));
}
