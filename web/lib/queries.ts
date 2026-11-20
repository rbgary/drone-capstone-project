import "server-only";
import { cache } from "react";
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { LOGIN } from "@/lib/routes";
import type { Database } from "@/types/database";
import { normalizeContent, PAGE_SPECS, type ContentObject, type PageKey, type PageSpec } from "@/lib/site-content";

export type FlightRow = Database["public"]["Tables"]["flights"]["Row"];
export type TelemetryRow = Database["public"]["Tables"]["telemetry"]["Row"];
export type MissionRow = Database["public"]["Tables"]["missions"]["Row"];
export type ZoneRow = Database["public"]["Tables"]["zones"]["Row"];
export type PredictionRow = Database["public"]["Tables"]["predictions"]["Row"];
export type ProfileRow = Database["public"]["Tables"]["profiles"]["Row"];
export type SessionRow = Database["public"]["Tables"]["sessions"]["Row"];
export type SessionSampleRow = Database["public"]["Tables"]["session_samples"]["Row"];
export type AuditEventRow = Database["public"]["Tables"]["audit_events"]["Row"];
export type PipelineResultRow = Database["public"]["Tables"]["pipeline_results"]["Row"];
export type FindingRow = Database["public"]["Tables"]["pipeline_findings"]["Row"];
export type SessionResultRow = Database["public"]["Tables"]["pipeline_session_results"]["Row"];
export type NotificationRow = Database["public"]["Tables"]["notifications"]["Row"];

/**
 * A read that failed, as opposed to a read that found nothing.
 *
 * Reads throw rather than returning `data ?? []`. An empty fallback turns a
 * database error into an empty list — "No flights recorded yet" when the truth
 * is "could not ask" — and silence is the dangerous state for an operator about
 * to fly. The route's `error.tsx` says what happened instead.
 *
 * The Postgres message is logged server-side and never sent to the browser: it
 * can carry column names and policy internals.
 */
export class QueryError extends Error {
  constructor(what: string, cause: { message: string; code?: string }) {
    console.error(`query failed: ${what}`, cause.code, cause.message);
    super(`Could not load ${what}.`);
    this.name = "QueryError";
  }
}

/**
 * An expired sign-in is not an error page. It is a sign-in.
 *
 * A session that runs out while a tab is open fails the NEXT read, and a read
 * that throws lands on error.tsx — which asks the operator to work out for
 * themselves that they need to sign in again. It also happens on a refresh
 * token that was rotated by a request running in parallel: two requests carry
 * the same token, the second is told it was already used, and a page that was
 * working a second ago throws.
 *
 * Either way the answer is the same, so it is taken here: send them to sign in
 * and bring them back. Anything they had typed is still in the browser, and
 * the editor offers it on return (components/admin/useDraft.ts).
 */
function signInAgain(cause: { message?: string; code?: string }, what: string): never {
  console.error(`sign-in expired while reading ${what}`, cause.code, cause.message);
  redirect(LOGIN);
}

function isExpiredSession(cause: { message?: string; code?: string }): boolean {
  const code = cause.code ?? "";
  const message = (cause.message ?? "").toLowerCase();
  return (
    code === "PGRST301" ||
    message.includes("jwt expired") ||
    message.includes("jwt is expired") ||
    message.includes("invalid refresh token") ||
    message.includes("refresh token not found") ||
    message.includes("already used")
  );
}

/** Every admin read ends here: sign in again, or say the read failed. */
function failed(cause: { message: string; code?: string }, what: string): never {
  if (isExpiredSession(cause)) signInAgain(cause, what);
  throw new QueryError(what, cause);
}

/**
 * Every read goes through this file.
 *
 * Two reasons it is worth the indirection. It keeps `createClient()` out of
 * components, so a later static export (a Tauri mobile build) swaps one file
 * instead of thirty. And it gives every query a single place to be cached and
 * typed.
 *
 * `cache()` memoises per request, so a layout and a page asking for the same
 * flight cost one round trip.
 */

export const getCurrentProfile = cache(async (): Promise<ProfileRow | null> => {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return null;

  const { data } = await supabase
    .from("profiles")
    .select("*")
    .eq("id", user.id)
    .maybeSingle();

  return data ?? null;
});

export const isOperator = cache(async (): Promise<boolean> => {
  const profile = await getCurrentProfile();
  return profile?.role === "operator";
});

export const getRecentFlights = cache(async (limit = 20): Promise<FlightRow[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("flights")
    .select("*")
    .order("started_at", { ascending: false })
    .limit(limit);
  if (error) failed(error, "flights");
  return data;
});

export const getFlight = cache(async (id: string): Promise<FlightRow | null> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("flights")
    .select("*")
    .eq("id", id)
    .maybeSingle();
  if (error) failed(error, "this flight");
  return data;
});

/**
 * Telemetry for one flight, in order.
 *
 * Capped: a 2-minute flight at 10 Hz is 1200 rows, but nothing stops someone
 * flying for ten minutes, and an uncapped select would send every row to the
 * browser to draw a chart a few hundred pixels wide.
 */
export const getFlightTelemetry = cache(
  async (flightId: string, limit = 5000): Promise<TelemetryRow[]> => {
    const supabase = await createClient();
    const { data, error } = await supabase
      .from("telemetry")
      .select("*")
      .eq("flight_id", flightId)
      .order("index", { ascending: true })
      .limit(limit);
    if (error) failed(error, "telemetry");
    return data;
  },
);

export const getZones = cache(async (): Promise<ZoneRow[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase.from("zones").select("*").order("label");
  if (error) failed(error, "zones");
  return data;
});

export const getMissions = cache(async (limit = 50): Promise<MissionRow[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("missions")
    .select("*")
    .order("created_at", { ascending: false })
    .limit(limit);
  if (error) failed(error, "missions");
  return data;
});

// ── sessions ─────────────────────────────────────────────────────────────
//
// A session is one operator, one drone, Start to End session — flying or
// not. Its own vitals (one a second, session_samples) cover the whole of it;
// its flights' telemetry covers only the time the motors ran.

/** A session in the list: who ran it, on which drone, and how many flights.
 *  Who ran it is on the row itself (operator_name, operator_email), stamped by
 *  the database (migration 20261009000015) — profiles stay "read your own". */
export type SessionSummary = SessionRow & {
  drone: string | null;
  flight_count: number;
};

export const getRecentSessions = cache(async (limit = 50): Promise<SessionSummary[]> => {
  const supabase = await createClient();
  const { data: sessions, error } = await supabase
    .from("sessions")
    .select("*")
    .order("started_at", { ascending: false })
    .limit(limit);
  if (error) failed(error, "sessions");
  if (sessions.length === 0) return [];
  const ids = sessions.map((s) => s.id);
  const [flights, drones] = await Promise.all([
    supabase.from("flights").select("session_id").in("session_id", ids),
    supabase.from("drones").select("id, name")
      .in("id", sessions.map((s) => s.drone_id).filter((x): x is string => !!x)),
  ]);
  if (flights.error) failed(flights.error, "session flights");
  const counts = new Map<string, number>();
  for (const f of flights.data) if (f.session_id) counts.set(f.session_id, (counts.get(f.session_id) ?? 0) + 1);
  const what = new Map((drones.data ?? []).map((d) => [d.id, d.name]));
  return sessions.map((s) => ({
    ...s,
    drone: s.drone_id ? what.get(s.drone_id) ?? null : null,
    flight_count: counts.get(s.id) ?? 0,
  }));
});

export const getSession = cache(async (id: string): Promise<SessionSummary | null> => {
  const supabase = await createClient();
  const { data, error } = await supabase.from("sessions").select("*").eq("id", id).maybeSingle();
  if (error) failed(error, "this session");
  if (!data) return null;
  const [flights, drone] = await Promise.all([
    supabase.from("flights").select("id", { count: "exact", head: true }).eq("session_id", id),
    data.drone_id
      ? supabase.from("drones").select("name").eq("id", data.drone_id).maybeSingle()
      : Promise.resolve({ data: null }),
  ]);
  return {
    ...data,
    drone: drone.data?.name ?? null,
    flight_count: flights.count ?? 0,
  };
});

/** The session's own vitals, one a second. `migrated: false` while the
 *  database has no session_samples table yet (migration 20261006000013) —
 *  said on the page, never shown as an error or as "nothing recorded". */
export const getSessionSamples = cache(
  async (sessionId: string, limit = 10000): Promise<{ rows: SessionSampleRow[]; migrated: boolean }> => {
    const supabase = await createClient();
    const { data, error } = await supabase
      .from("session_samples")
      .select("*")
      .eq("session_id", sessionId)
      .order("seq", { ascending: true })
      .limit(limit);
    if (error) {
      if (error.code === "PGRST205" || error.code === "42P01" || error.message.includes("session_samples")) {
        return { rows: [], migrated: false };
      }
      failed(error, "session vitals");
    }
    return { rows: data, migrated: true };
  },
);

export const getSessionFlights = cache(async (sessionId: string): Promise<FlightRow[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("flights")
    .select("*")
    .eq("session_id", sessionId)
    .order("started_at", { ascending: true });
  if (error) failed(error, "this session's flights");
  return data;
});

/** What happened in the session: checks, starts, landings, refusals. Read by
 *  operators only (row-level security) — empty for anyone else. */
export const getSessionEvents = cache(async (sessionId: string): Promise<AuditEventRow[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("audit_events")
    .select("*")
    .eq("session_id", sessionId)
    .order("occurred_at", { ascending: true })
    .limit(500);
  if (error) failed(error, "this session's events");
  return data;
});

// ── the data pipeline's results (migration 20261009000016) ────────────────

/** A table this deployment's database does not have yet — said on the page
 *  ("apply migration …"), never shown as an error or as "nothing". */
function notMigrated(error: { code?: string; message: string }, table: string): boolean {
  return error.code === "PGRST205" || error.code === "42P01" || error.message.includes(table);
}

/** Every flight's pipeline result, by flight. */
export const getPipelineResults = cache(
  async (flightIds: readonly string[]): Promise<{ rows: PipelineResultRow[]; migrated: boolean }> => {
    if (flightIds.length === 0) return { rows: [], migrated: true };
    const supabase = await createClient();
    const { data, error } = await supabase.from("pipeline_results").select("*")
      .in("flight_id", [...flightIds]);
    if (error) {
      if (notMigrated(error, "pipeline_results")) return { rows: [], migrated: false };
      failed(error, "pipeline results");
    }
    return { rows: data, migrated: true };
  },
);

/** Every finding of these flights, worst first, then in time. */
export const getFindings = cache(
  async (flightIds: readonly string[]): Promise<{ rows: FindingRow[]; migrated: boolean }> => {
    if (flightIds.length === 0) return { rows: [], migrated: true };
    const supabase = await createClient();
    const { data, error } = await supabase.from("pipeline_findings").select("*")
      .in("flight_id", [...flightIds]).eq("scope", "flight").order("t_start_s", { ascending: true });
    if (error) {
      if (notMigrated(error, "pipeline_findings")) return { rows: [], migrated: false };
      failed(error, "findings");
    }
    const rank = { critical: 2, warning: 1, info: 0 } as Record<string, number>;
    return { rows: [...data].sort((a, b) => (rank[b.severity] ?? 0) - (rank[a.severity] ?? 0)),
             migrated: true };
  },
);

/** A session's own result — its samples around the flights, on the ground
 *  (migration 20261009000018). `migrated: false` while the table is missing. */
export const getSessionResult = cache(
  async (sessionId: string): Promise<{ row: SessionResultRow | null; migrated: boolean }> => {
    const supabase = await createClient();
    const { data, error } = await supabase.from("pipeline_session_results").select("*")
      .eq("session_id", sessionId).maybeSingle();
    if (error) {
      if (notMigrated(error, "pipeline_session_results")) return { row: null, migrated: false };
      failed(error, "the session's result");
    }
    return { row: data, migrated: true };
  },
);

/** A session's own findings (scope "session"), worst first, then in time. */
export const getSessionFindings = cache(
  async (sessionId: string): Promise<{ rows: FindingRow[]; migrated: boolean }> => {
    const supabase = await createClient();
    const { data, error } = await supabase.from("pipeline_findings").select("*")
      .eq("session_id", sessionId).eq("scope", "session").order("t_start_s", { ascending: true });
    if (error) {
      if (notMigrated(error, "scope")) return { rows: [], migrated: false };
      failed(error, "the session's findings");
    }
    const rank = { critical: 2, warning: 1, info: 0 } as Record<string, number>;
    return { rows: [...data].sort((a, b) => (rank[b.severity] ?? 0) - (rank[a.severity] ?? 0)),
             migrated: true };
  },
);

// ── notifications (migration 20261009000017) ─────────────────────────────

/** The signed-in operator's notifications, newest first; `before` pages back.
 *  Row-level security returns only their own — the filter on user_id is for
 *  the index, not the guard. */
export const getNotifications = cache(
  async (limit = 30, before?: string): Promise<{ rows: NotificationRow[]; migrated: boolean }> => {
    const profile = await getCurrentProfile();
    if (!profile) return { rows: [], migrated: true };
    const supabase = await createClient();
    let query = supabase.from("notifications").select("*").eq("user_id", profile.id)
      .order("created_at", { ascending: false }).limit(limit);
    if (before) query = query.lt("created_at", before);
    const { data, error } = await query;
    if (error) {
      if (notMigrated(error, "notifications")) return { rows: [], migrated: false };
      failed(error, "notifications");
    }
    return { rows: data, migrated: true };
  },
);

/** How many of the operator's notifications are unread. */
export const getUnreadCount = cache(async (): Promise<number> => {
  const profile = await getCurrentProfile();
  if (!profile) return 0;
  const supabase = await createClient();
  const { count, error } = await supabase.from("notifications")
    .select("id", { count: "exact", head: true })
    .eq("user_id", profile.id).is("read_at", null);
  if (error) {
    if (notMigrated(error, "notifications")) return 0;
    failed(error, "unread notifications");
  }
  return count ?? 0;
});

/** Each flight's telemetry, capped per flight like the flight page. */
export const getFlightsTelemetry = cache(
  async (flightIds: readonly string[], perFlight = 5000): Promise<Record<string, TelemetryRow[]>> => {
    const all = await Promise.all(flightIds.map((id) => getFlightTelemetry(id, perFlight)));
    return Object.fromEntries(flightIds.map((id, i) => [id, all[i]]));
  },
);

export type DroneRow = Database["public"]["Tables"]["drones"]["Row"];

export const getDrones = cache(async (): Promise<DroneRow[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase.from("drones").select("*").order("name");
  if (error) failed(error, "drones");
  return data;
});

/**
 * The flight in progress, if any.
 *
 * Newest `running` flight only. A flight the agent never closed (a crash
 * mid-flight) stays `running` forever, so the live page shows its start time
 * and lets the operator judge whether it is really in the air.
 */
export const getRunningFlight = cache(async (): Promise<FlightRow | null> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("flights")
    .select("*")
    .eq("status", "running")
    .order("started_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (error) failed(error, "the running flight");
  return data;
});

/**
 * The most recent health estimate for each zone, keyed by zone id.
 *
 * "Most recent" per zone, not "from the most recent flight": a flight that
 * covered only half the greenhouse must not blank the other half's estimates.
 * Bounded, newest first, reduced here — a zone missing from the result has no
 * estimate in that window, and the map says so.
 */
export const getLatestZoneHealth = cache(
  async (limit = 1000): Promise<Map<string, PredictionRow>> => {
    const supabase = await createClient();
    const { data, error } = await supabase
      .from("predictions")
      .select("*")
      .not("zone_id", "is", null)
      .order("created_at", { ascending: false })
      .limit(limit);
    if (error) failed(error, "zone health");

    const latest = new Map<string, PredictionRow>();
    for (const row of data) {
      if (row.zone_id && !latest.has(row.zone_id)) latest.set(row.zone_id, row);
    }
    return latest;
  },
);

/** Dashboard summary. One call, so the landing view is a single round trip. */
export const getDashboardSummary = cache(async () => {
  const [flights, zones, missions] = await Promise.all([
    getRecentFlights(5),
    getZones(),
    getMissions(5),
  ]);

  const latest = flights[0] ?? null;
  const telemetry = latest ? await getFlightTelemetry(latest.id, 2000) : [];

  return {
    latest,
    flightCount: flights.length,
    zoneCount: zones.length,
    queued: missions.filter((m) => m.status === "queued").length,
    sampleCount: telemetry.length,
    telemetry,
  };
});

// ── portfolio ────────────────────────────────────────────────────────────
//
// Ported from ../doctor-portfolio. The public pages must never blank: Vercel
// deploys code before a hand-run migration reaches the database, so the public
// readers return an empty list when the tables are missing (Postgres 42P01 /
// PostgREST PGRST205) and throw on anything else.

export type ProjectRow = Database["public"]["Tables"]["projects"]["Row"];
export type TeamMemberRow = Database["public"]["Tables"]["team_members"]["Row"];
export type ProjectCard = Omit<ProjectRow, "content">;
export type ProjectWithTeam = ProjectRow & { team: TeamMemberRow[] };

const CARD_COLUMNS =
  "id, slug, title, subtitle, summary, cover_image_url, category, date_label, location, status, display_order, published_at, created_at, updated_at";

function missingTable(error: { code?: string }): boolean {
  return error.code === "42P01" || error.code === "PGRST205";
}

export const getPublishedProjects = cache(async (): Promise<ProjectCard[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("projects")
    .select(CARD_COLUMNS)
    .eq("status", "published")
    .order("display_order", { ascending: true })
    .order("created_at", { ascending: false });
  if (error) {
    if (missingTable(error)) return [];
    failed(error, "projects");
  }
  return data;
});

/** Team members, in display order. */
export const getTeamMembers = cache(async (): Promise<TeamMemberRow[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("team_members")
    .select("*")
    .order("display_order", { ascending: true })
    .order("created_at", { ascending: true });
  if (error) {
    if (missingTable(error)) return [];
    failed(error, "the team");
  }
  return data;
});

/** One person, by the slug in their URL. */
export const getTeamMemberBySlug = cache(
  async (slug: string): Promise<TeamMemberRow | null> => {
    const supabase = await createClient();
    const { data, error } = await supabase
      .from("team_members")
      .select("*")
      .eq("slug", slug)
      .maybeSingle();
    if (error) {
      if (missingTable(error)) return null;
      failed(error, "this team member");
    }
    return data;
  },
);

/**
 * The published projects a person worked on, in the order they are listed on
 * each project. A draft is filtered out here as well as by RLS: an operator
 * reading a public page should see the public page.
 */
export const getProjectsForMember = cache(async (memberId: string): Promise<ProjectCard[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("project_members")
    .select(`display_order, projects(${CARD_COLUMNS})`)
    .eq("member_id", memberId)
    .order("display_order", { ascending: true });
  if (error) {
    if (missingTable(error)) return [];
    failed(error, "this person's projects");
  }
  return data
    .map((row) => row.projects as ProjectCard | null)
    .filter((p): p is ProjectCard => p !== null && p.status === "published");
});

async function teamFor(projectId: string): Promise<TeamMemberRow[]> {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("project_members")
    .select("display_order, team_members(*)")
    .eq("project_id", projectId)
    .order("display_order", { ascending: true });
  if (error) failed(error, "this project's team");
  return data
    .map((row) => row.team_members as TeamMemberRow | null)
    .filter((m): m is TeamMemberRow => m !== null);
}

export const getPublishedProjectBySlug = cache(
  async (slug: string): Promise<ProjectWithTeam | null> => {
    const supabase = await createClient();
    const { data, error } = await supabase
      .from("projects")
      .select("*")
      .eq("slug", slug)
      .eq("status", "published")
      .maybeSingle();
    if (error) {
      if (missingTable(error)) return null;
      failed(error, "this project");
    }
    if (!data) return null;
    return { ...data, team: await teamFor(data.id) };
  },
);

/** Admin: every project, drafts included. RLS shows drafts to operators only. */
export const getAllProjectsAdmin = cache(async (): Promise<ProjectCard[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("projects")
    .select(CARD_COLUMNS)
    .order("display_order", { ascending: true })
    .order("created_at", { ascending: false });
  if (error) failed(error, "projects");
  return data;
});

export const getProjectByIdAdmin = cache(
  async (id: string): Promise<ProjectWithTeam | null> => {
    const supabase = await createClient();
    const { data, error } = await supabase.from("projects").select("*").eq("id", id).maybeSingle();
    if (error) failed(error, "this project");
    if (!data) return null;
    return { ...data, team: await teamFor(data.id) };
  },
);

// ── gallery ──────────────────────────────────────────────────────────────
//
// Albums of photos and videos (migration 0008). Same rule as the portfolio:
// public readers return empty when the tables are missing, admin readers throw.

export type AlbumRow = Database["public"]["Tables"]["gallery_albums"]["Row"];
export type GalleryItemRow = Database["public"]["Tables"]["gallery_items"]["Row"];
export type AlbumCard = AlbumRow & {
  photoCount: number;
  videoCount: number;
  /** Up to three images for the card's mosaic, in album order. */
  preview: GalleryItemRow[];
};
export type AlbumWithItems = AlbumRow & { items: GalleryItemRow[] };

function toCard(album: AlbumRow & { gallery_items: GalleryItemRow[] | null }): AlbumCard {
  const items = [...(album.gallery_items ?? [])].sort((a, b) => a.display_order - b.display_order);
  const { gallery_items: _items, ...row } = album;
  void _items;
  return {
    ...row,
    photoCount: items.filter((i) => i.kind === "image").length,
    videoCount: items.filter((i) => i.kind === "video").length,
    preview: items.filter((i) => i.kind === "image").slice(0, 3),
  };
}

export const getPublishedAlbums = cache(async (): Promise<AlbumCard[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("gallery_albums")
    .select("*, gallery_items(*)")
    .eq("status", "published")
    .order("display_order", { ascending: true });
  if (error) {
    if (missingTable(error)) return [];
    failed(error, "the gallery");
  }
  return data.map(toCard);
});

export const getPublishedAlbumBySlug = cache(async (slug: string): Promise<AlbumWithItems | null> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("gallery_albums")
    .select("*, gallery_items(*)")
    .eq("slug", slug)
    .eq("status", "published")
    .maybeSingle();
  if (error) {
    if (missingTable(error)) return null;
    failed(error, "this album");
  }
  if (!data) return null;
  const { gallery_items, ...album } = data;
  return { ...album, items: [...(gallery_items ?? [])].sort((a, b) => a.display_order - b.display_order) };
});

export const getAllAlbumsAdmin = cache(async (): Promise<AlbumCard[]> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("gallery_albums")
    .select("*, gallery_items(*)")
    .order("display_order", { ascending: true });
  if (error) failed(error, "albums");
  return data.map(toCard);
});

export const getAlbumByIdAdmin = cache(async (id: string): Promise<AlbumWithItems | null> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("gallery_albums")
    .select("*, gallery_items(*)")
    .eq("id", id)
    .maybeSingle();
  if (error) failed(error, "this album");
  if (!data) return null;
  const { gallery_items, ...album } = data;
  return { ...album, items: [...(gallery_items ?? [])].sort((a, b) => a.display_order - b.display_order) };
});

// ── editable page wording ────────────────────────────────────────────────

/**
 * A public page's wording: the stored override normalised against the page's
 * spec, or its defaults. Never throws — a public page must render even if the
 * table is missing or the read fails; the failure is logged.
 */
export const getPageContent = cache(async (key: PageKey): Promise<ContentObject> => {
  const supabase = await createClient();
  const { data, error } = await supabase
    .from("site_pages").select("content, updated_at").eq("key", key).maybeSingle();
  if (error && !missingTable(error)) console.error(`page content read failed: ${key}`, error.code, error.message);
  return normalizeContent(key, overrideStillCurrent(key, data) ? data?.content ?? null : null);
});

/** Is a stored override newer than the page's last rewrite? (PageSpec.defaultsRevised) */
function overrideStillCurrent(key: PageKey, row: { updated_at: string } | null): boolean {
  const revised: string | undefined = (PAGE_SPECS[key] as PageSpec).defaultsRevised;
  if (!row || !revised) return true;
  return new Date(row.updated_at).getTime() >= new Date(revised).getTime();
}

/** Admin: which pages carry an override, and when each was last edited. */
export const getEditedPages = cache(async (): Promise<Map<string, string>> => {
  const supabase = await createClient();
  const { data, error } = await supabase.from("site_pages").select("key, updated_at");
  if (error) failed(error, "the pages");
  return new Map(data.map((row) => [row.key, row.updated_at]));
});

// ── the Processed data pages ─────────────────────────────────────────────

/** A processed flight in the overview: its result's headline, not its JSON. */
export type ProcessedFlight = Pick<PipelineResultRow,
  "flight_id" | "session_id" | "findings_count" | "worst_severity" | "created_at" | "pipeline_version">;

/** A session with processed flights: who ran it, and how far processing got. */
export type ProcessedSession = SessionRow & {
  flights: number;
  processed: number;
  findings: number;
  worst: string | null;
  /** The session itself was processed (its samples around the flights). */
  ownResult: boolean;
};

/**
 * Everything the pipeline has processed, newest first — the Processed data
 * page's list. `migrated: false` while the database has no pipeline tables.
 * Findings come worst first, then newest; `limit` caps them, and the page says
 * when it did.
 */
export const getProcessedOverview = cache(async (limit = 200): Promise<{
  migrated: boolean;
  flights: ProcessedFlight[];
  sessions: ProcessedSession[];
  findings: FindingRow[];
  findingsTotal: number;
}> => {
  const supabase = await createClient();
  const results = await supabase.from("pipeline_results")
    .select("flight_id, session_id, findings_count, worst_severity, created_at, pipeline_version")
    .order("created_at", { ascending: false }).limit(2000);
  if (results.error) {
    if (notMigrated(results.error, "pipeline_results")) {
      return { migrated: false, flights: [], sessions: [], findings: [], findingsTotal: 0 };
    }
    failed(results.error, "processed flights");
  }
  const findings = await supabase.from("pipeline_findings").select("*", { count: "exact" })
    .order("created_at", { ascending: false }).limit(limit);
  if (findings.error) {
    if (notMigrated(findings.error, "pipeline_findings")) {
      return { migrated: false, flights: [], sessions: [], findings: [], findingsTotal: 0 };
    }
    failed(findings.error, "findings");
  }
  // A session processed on its own counts too (migration 20261009000018).
  const own = await supabase.from("pipeline_session_results")
    .select("session_id, findings_count, worst_severity");
  const sessionResults = own.error ? [] : own.data;
  const sessionIds = [...new Set([
    ...results.data.map((r) => r.session_id),
    ...sessionResults.map((r) => r.session_id),
  ].filter((x): x is string => !!x))];
  let sessions: ProcessedSession[] = [];
  if (sessionIds.length) {
    const [rows, flights] = await Promise.all([
      supabase.from("sessions").select("*").in("id", sessionIds).order("started_at", { ascending: false }),
      supabase.from("flights").select("session_id").in("session_id", sessionIds),
    ]);
    if (rows.error) failed(rows.error, "sessions");
    if (flights.error) failed(flights.error, "session flights");
    const count = (id: string) => flights.data.filter((f) => f.session_id === id).length;
    const rank = { critical: 2, warning: 1, info: 0 } as Record<string, number>;
    sessions = rows.data.map((s) => {
      const mine = [...results.data.filter((r) => r.session_id === s.id),
                    ...sessionResults.filter((r) => r.session_id === s.id)];
      const worst = mine.reduce<string | null>((w, r) => (r.worst_severity
        && (w === null || (rank[r.worst_severity] ?? 0) > (rank[w] ?? 0)) ? r.worst_severity : w), null);
      return { ...s, flights: count(s.id),
               processed: results.data.filter((r) => r.session_id === s.id).length,
               findings: mine.reduce((n, r) => n + r.findings_count, 0), worst,
               ownResult: sessionResults.some((r) => r.session_id === s.id) };
    });
  }
  const rank = { critical: 2, warning: 1, info: 0 } as Record<string, number>;
  return {
    migrated: true,
    flights: results.data,
    sessions,
    findings: [...findings.data].sort((a, b) => (rank[b.severity] ?? 0) - (rank[a.severity] ?? 0)),
    findingsTotal: findings.count ?? findings.data.length,
  };
});

/** The readings a stretch covers (telemetry `index`, inclusive) — the
 *  evidence an anomaly's card shows. Finding id → rows, in order. */
export const getStretchReadings = cache(
  async (findings: readonly Pick<FindingRow, "id" | "flight_id" | "session_id" | "start_index" | "end_index">[],
  ): Promise<Record<string, { index: number; recorded_at: string; [key: string]: unknown }[]>> => {
    if (findings.length === 0) return {};
    const supabase = await createClient();
    const out: Record<string, { index: number; recorded_at: string; [key: string]: unknown }[]> = {};
    await Promise.all(findings.map(async (f) => {
      if (f.flight_id) {
        const r = await supabase.from("telemetry").select("*")
          .eq("flight_id", f.flight_id).gte("index", f.start_index).lte("index", f.end_index)
          .order("index", { ascending: true }).limit(5000);
        if (r.error) failed(r.error, "an anomaly's readings");
        out[f.id] = r.data;
      } else if (f.session_id) {
        // A session's finding: its samples, one a second, by seq.
        const r = await supabase.from("session_samples").select("*")
          .eq("session_id", f.session_id).gte("seq", f.start_index).lte("seq", f.end_index)
          .order("seq", { ascending: true }).limit(5000);
        if (r.error) failed(r.error, "an anomaly's readings");
        out[f.id] = r.data.map((row) => ({ ...row, index: row.seq }));
      }
    }));
    return out;
  },
);
