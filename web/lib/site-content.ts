/**
 * The wording of every public page, editable from /admin/pages.
 *
 * Each page is described once here: a field spec (what can be edited, and how
 * the admin form draws it) and its default wording (what the page said before it
 * was editable, copied verbatim). The database stores only overrides
 * (`site_pages`, migration 0009):
 *
 *   - a page with no row shows its defaults, so nothing can blank for want of a row
 *   - everything read or saved goes through `normalizeContent`, which keeps only
 *     the fields the spec declares, of the declared type — a malformed row or a
 *     crafted request cannot put anything else on a public page
 *
 * Shared by the server (pages, mutations) and the client (the admin form), so it
 * imports nothing server-only.
 */

export type FieldSpec =
  | { type: "text"; key: string; label: string; hint?: string }
  | { type: "textarea"; key: string; label: string; hint?: string }
  | { type: "boolean"; key: string; label: string; hint?: string }
  | { type: "image"; key: string; label: string; hint?: string }
  | { type: "strings"; key: string; label: string; itemLabel: string; hint?: string }
  | { type: "items"; key: string; label: string; itemLabel: string; fields: FieldSpec[]; hint?: string };

export type PageSpec = {
  key: PageKey;
  title: string;
  description: string;
  path: string;
  fields: FieldSpec[];
  /**
   * When this page's default wording was last rewritten (ISO date). A stored
   * override saved BEFORE it is set aside and the defaults shown instead: the
   * override was an edit of the old wording, and keeping it would hide the
   * rewrite from every visitor (the `setup` row did exactly that for a week).
   * An edit saved after it wins as usual.
   */
  defaultsRevised?: string;
};

export type ContentValue = string | boolean | string[] | ContentObject[];
export type ContentObject = { [key: string]: ContentValue };

// ── defaults: the wording the pages shipped with ─────────────────────────

const HEADER_FIELDS: FieldSpec[] = [
  { type: "text", key: "eyebrow", label: "Small label above the title" },
  { type: "text", key: "title", label: "Title" },
  { type: "textarea", key: "intro", label: "Introduction" },
];

/** Every setup guide is the same form: numbered steps, then what goes wrong. */
const SETUP_FIELDS: FieldSpec[] = [
  { type: "text", key: "title", label: "Title" },
  { type: "textarea", key: "intro", label: "Introduction" },
  {
    type: "items", key: "steps", label: "Steps", itemLabel: "Step",
    fields: [
      { type: "text", key: "title", label: "Step title" },
      { type: "strings", key: "paragraphs", label: "Paragraphs", itemLabel: "Paragraph" },
      { type: "strings", key: "needs", label: "What you need", itemLabel: "Requirement",
        hint: "A checklist, one thing per line, shown before the commands." },
      { type: "strings", key: "commands", label: "Terminal commands", itemLabel: "Command",
        hint: "One command per line, in order. Each gets its own copy button." },
      { type: "strings", key: "bullets", label: "Bullet points", itemLabel: "Bullet" },
      { type: "textarea", key: "note", label: "Highlighted note", hint: "Shown in a shaded box. Leave empty for none." },
      { type: "boolean", key: "showDownloads", label: "Show this product's download buttons" },
      { type: "boolean", key: "linkHardware", label: "Link to the Hardware page" },
    ],
  },
  { type: "text", key: "commandsTitle", label: "Useful commands title" },
  { type: "textarea", key: "commandsIntro", label: "Useful commands introduction" },
  {
    type: "items", key: "reference", label: "Useful commands", itemLabel: "Command",
    hint: "Shown after the steps, grouped under each distinct group name, in order.",
    fields: [
      { type: "text", key: "group", label: "Group", hint: "Commands with the same group are listed together." },
      { type: "text", key: "command", label: "Command", hint: "Exactly what to type. It gets a copy button." },
      { type: "text", key: "what", label: "What it does" },
    ],
  },
  { type: "text", key: "troubleTitle", label: "Troubleshooting title" },
  { type: "textarea", key: "troubleIntro", label: "Troubleshooting introduction" },
  {
    type: "items", key: "troubles", label: "Problems", itemLabel: "Problem",
    fields: [
      { type: "text", key: "symptom", label: "What you see" },
      { type: "text", key: "cause", label: "Usual cause" },
      { type: "textarea", key: "fix", label: "What to do" },
    ],
  },
];

export const PAGE_SPECS = {
  site: {
    key: "site",
    title: "Whole site",
    description: "The footer shown on every visitor page.",
    path: "/",
    fields: [
      { type: "textarea", key: "footerText", label: "Footer text" },
      { type: "text", key: "footerLinkLabel", label: "Footer link label", hint: "Links to the Set Up page." },
    ],
  },
  home: {
    key: "home",
    title: "Home",
    description: "The landing page every visitor sees first.",
    path: "/",
    defaultsRevised: "2026-09-28T00:00:00Z",
    fields: [
      { type: "text", key: "eyebrow", label: "Small label above the name" },
      { type: "text", key: "title", label: "Tagline", hint: "The line under the DroneDeck name." },
      { type: "textarea", key: "intro", label: "Introduction" },
      { type: "text", key: "primaryCta", label: "Main button", hint: "Goes to Projects." },
      { type: "text", key: "secondaryCta", label: "Second button", hint: "Goes to the dashboard (sign-in first)." },
      { type: "image", key: "heroImage", label: "Main photo", hint: "Beside the title. Leave empty for none." },
      { type: "text", key: "heroImageAlt", label: "Main photo description", hint: "Read aloud by screen readers." },
      {
        type: "items", key: "stats", label: "Numbers", itemLabel: "Number",
        hint: "A row of figures under the introduction. Each is a value and what it counts.",
        fields: [
          { type: "text", key: "value", label: "Value" },
          { type: "text", key: "label", label: "What it counts" },
        ],
      },
      { type: "text", key: "capabilitiesTitle", label: "Title above the part cards" },
      { type: "textarea", key: "capabilitiesIntro", label: "Text above the part cards" },
      {
        type: "items", key: "capabilities", label: "Part cards", itemLabel: "Card",
        fields: [
          { type: "image", key: "image", label: "Photo", hint: "Optional. Sits above the title." },
          { type: "text", key: "title", label: "Title" },
          { type: "text", key: "status", label: "Status", hint: "Built, In progress or Planned — shown as a tag." },
          { type: "textarea", key: "body", label: "Text" },
          { type: "text", key: "href", label: "Link", hint: "Where the card goes, e.g. /projects/autonomous-flight. Empty for none." },
        ],
      },
      { type: "text", key: "roadmapTitle", label: "Roadmap title" },
      { type: "textarea", key: "roadmapIntro", label: "Roadmap introduction" },
      {
        type: "items", key: "roadmap", label: "Roadmap", itemLabel: "Stage",
        fields: [
          { type: "text", key: "when", label: "When", hint: "e.g. Sprint 1 · 28 Sep – 11 Oct" },
          { type: "text", key: "title", label: "Title" },
          { type: "text", key: "status", label: "Status", hint: "Done, Now or Next — shown as a tag." },
          { type: "textarea", key: "body", label: "What happens" },
        ],
      },
      { type: "text", key: "planTitle", label: "Plan document title" },
      { type: "textarea", key: "planBody", label: "Plan document text" },
      { type: "text", key: "planLinkLabel", label: "Plan document link label" },
      { type: "text", key: "planFile", label: "Plan document file", hint: "A site path, e.g. /docs/dronedeck-project-management-planning.pdf" },
      { type: "boolean", key: "showGallery", label: "Show a strip of photos from the gallery" },
      { type: "text", key: "galleryTitle", label: "Title of the photo strip" },
      { type: "text", key: "teamLine", label: "Line above the footer" },
      { type: "text", key: "teamLinkLabel", label: "Team link label" },
    ],
  },
  projects: {
    key: "projects", title: "Projects", description: "The heading of the projects list.",
    path: "/projects", fields: HEADER_FIELDS,
  },
  team: {
    key: "team", title: "Team", description: "The heading of the team page.",
    path: "/team", fields: HEADER_FIELDS,
  },
  gallery: {
    key: "gallery", title: "Gallery", description: "The heading of the gallery.",
    path: "/gallery", fields: HEADER_FIELDS,
  },
  apps: {
    key: "apps",
    title: "Apps",
    description: "The download page. The builds themselves come from CI, not from here.",
    path: "/apps",
    fields: [
      ...HEADER_FIELDS,
      { type: "textarea", key: "note", label: "Note above the downloads", hint: "Shown in a shaded box." },
      { type: "text", key: "unsignedTitle", label: "Heading of the 'it will warn you' section" },
      { type: "textarea", key: "unsignedIntro", label: "Text of that section" },
    ],
  },
  "setup-index": {
    key: "setup-index",
    title: "Set Up — the index",
    description: "The heading above the setup cards. Each guide has its own page.",
    path: "/setup",
    fields: HEADER_FIELDS,
  },
  // Key "setup-desktop", not "setup": a `setup` row saved 2026-09-21 (the old
  // wording plus an "EDITED" test) held the whole page and hid every change to
  // these defaults. Renaming the key on 2026-09-28 retired it; that row is
  // no longer read by anything.
  "setup-desktop": {
    key: "setup-desktop",
    title: "Set Up — desktop app",
    description: "From an unopened box to a first flight, and what to do when it will not fly.",
    path: "/setup/desktop-app",
    fields: SETUP_FIELDS,
  },
  "setup-web": {
    key: "setup-web",
    title: "Set Up — dashboard",
    description: "Running this website locally, and what it needs to be deployed.",
    path: "/setup/dashboard",
    fields: SETUP_FIELDS,
  },
  hardware: {
    key: "hardware",
    title: "Hardware",
    description: "Every part of the kit, photographed and labelled.",
    path: "/setup/hardware",
    fields: [
      { type: "text", key: "title", label: "Title" },
      { type: "textarea", key: "intro", label: "Introduction" },
      { type: "text", key: "sensorsTitle", label: "Highlighted section title" },
      { type: "strings", key: "sensorsParagraphs", label: "Highlighted section paragraphs", itemLabel: "Paragraph" },
      {
        type: "items", key: "parts", label: "Parts", itemLabel: "Part",
        fields: [
          { type: "image", key: "image", label: "Photo" },
          { type: "text", key: "alt", label: "Photo description", hint: "Read aloud by screen readers." },
          { type: "text", key: "name", label: "Name" },
          { type: "textarea", key: "body", label: "Description" },
        ],
      },
    ],
  },
} satisfies Record<string, PageSpec>;

export type PageKey =
  | "site" | "home" | "projects" | "team" | "gallery" | "apps"
  | "setup-index" | "setup-desktop" | "setup-web" | "hardware";
export const PAGE_KEYS = Object.keys(PAGE_SPECS) as PageKey[];

export function isPageKey(value: string): value is PageKey {
  return (PAGE_KEYS as string[]).includes(value);
}

export const PAGE_DEFAULTS: Record<PageKey, ContentObject> = {
  site: {
    footerText:
      "DroneDeck — Team 18's capstone: an autonomous indoor inspection drone on a Crazyflie 2.1.",
    footerLinkLabel: "Set up the system",
  },
  home: {
    eyebrow: "Team 18 · Capstone project",
    title: "An autonomous drone that inspects the site it is deployed in.",
    intro:
      "DroneDeck flies a Crazyflie 2.1 through a route of inspection points, records temperature, " +
      "pressure and images at each one, and flags equipment that looks wrong — on a desktop app " +
      "that flies it, and a dashboard that shows what it found.",
    primaryCta: "See how it is built",
    secondaryCta: "Dashboard",
    heroImage: "/brand/drone-hero.webp",
    heroImageAlt: "A grey quadcopter drone hovering against a white background",
    stats: [
      { value: "41", label: "User stories · 34 MVP, 7 stretch" },
      { value: "13", label: "Stories built before Sprint 1" },
      { value: "689", label: "Person-hours planned" },
      { value: "695", label: "Automated tests on the flight code" },
    ],
    capabilitiesTitle: "Six parts, one system",
    capabilitiesIntro:
      "Our planning document splits the work into six parts. The apps and the flight software " +
      "under them are built; autonomous flight and the data pipeline are this sprint's work. " +
      "Each part has its own page: what it does, how it was built or will be, who owns it, " +
      "and what is left.",
    capabilities: [
      {
        image: "/projects/apps.webp",
        title: "Apps, manual flight and live view",
        status: "Built",
        body:
          "A desktop app that flies the drone and a website with an operator dashboard, one " +
          "account for both. Keyboard flight at 50 Hz that holds its position, live sensors, " +
          "the drone's camera, and every flight saved for comparison.",
        href: "/projects/apps-and-manual-flight",
      },
      {
        image: "/projects/autonomous-flight.webp",
        title: "Autonomous flight and navigation",
        status: "In progress",
        body:
          "Take off, fly a planned route from one inspection point to the next, and land where " +
          "it started — inside a virtual fence, on Lighthouse positioning. The fence and the route " +
          "planner are built; the flights are Sprint 1.",
        href: "/projects/autonomous-flight",
      },
      {
        image: "/projects/data-collection.webp",
        title: "Data collection",
        status: "In progress",
        body:
          "Temperature, pressure and position ten times a second, greyscale images from the AI " +
          "deck, written to disk first and uploaded after. Next: capturing at each inspection " +
          "point and tagging every reading with its point.",
        href: "/projects/data-collection",
      },
      {
        image: "/projects/data-processing.webp",
        title: "Anomaly detection and alerts",
        status: "In progress",
        body:
          "One pipeline per flight — clean the readings, sharpen the images, classify, " +
          "interpret — ending in a verdict for every inspection point and an alert with its " +
          "reason. Training data and the first classifier start in Sprint 1.",
        href: "/projects/anomaly-detection",
      },
      {
        image: "/projects/documentation.webp",
        title: "Documentation",
        status: "In progress",
        body:
          "Setup, operating and waypoint guides written so someone outside the team can run a " +
          "mission, plus a record of every decision and problem, kept each sprint.",
        href: "/projects/documentation",
      },
      {
        image: "/projects/testing.webp",
        title: "Testing and experiment design",
        status: "In progress",
        body:
          "Boxes standing in for equipment, a written definition of faulty, a hand warmer for " +
          "an abnormal reading, and a thermometer and barometer to check the drone against.",
        href: "/projects/testing",
      },
    ],
    roadmapTitle: "Where the project stands",
    roadmapIntro:
      "Sprint by sprint, from our planning document. Sprint 0 is what was built before the plan " +
      "was written; Sprint 1 is the backlog we committed to on 28 September. Later sprints show " +
      "the order the remaining stories are planned in — the dates for each task are in our Jira " +
      "timeline.",
    roadmap: [
      {
        when: "Sprint 0 · 16–27 Sep",
        status: "Done",
        title: "The apps and the flight software under them",
        body:
          "Website and desktop app, one sign-in with an operator role enforced in the database, " +
          "keyboard flight that holds its position, live sensor windows, the live camera, " +
          "flight history and comparison, pre-flight checks with an audit trail, a one-command " +
          "install on macOS, Windows and Linux, the virtual fence, the mission planner, and " +
          "recording plus upload of every reading and frame.",
      },
      {
        when: "Sprint 1 · 28 Sep – 11 Oct",
        status: "Now",
        title: "First autonomous flights, the test rig and the first model",
        body:
          "Hannah and Samuel: measure the base stations, fly take-off → Point A → Point B → land " +
          "three times in a row. Hannah and Yordanos: build the test boxes, define faulty, take " +
          "reference readings and photograph the boxes for training. Kevin: clean bad sensor " +
          "readings, sharpen and colourise frames. Reagan: sharpen frames, train the first " +
          "faulty / not-faulty image classifier.",
      },
      {
        when: "Sprint 2 · 12–25 Oct",
        status: "Next",
        title: "Capture at every point, and one pipeline",
        body:
          "Return to base at the end of every mission; capture images and readings while holding " +
          "at each inspection point and tag them with the point's ID; one command that runs a " +
          "recorded flight through the pipeline; thresholds and a sensor classifier that flag " +
          "anomalies with a reason; hand-warmer tests; the setup guide.",
      },
      {
        when: "Sprint 3 · 26 Oct – 8 Nov",
        status: "Next",
        title: "Missions from the website, alerts on the dashboard",
        body:
          "The flight agent picks up routes queued on the website; flagged equipment is " +
          "highlighted on the dashboard and each alert opens to its reading or image and " +
          "reason; the operating guide; a full mission over the test boxes, end to end.",
      },
      {
        when: "Sprint 4 · 9–22 Nov",
        status: "Next",
        title: "The waypoint guide, and the first stretch goals",
        body:
          "Marker and waypoint guide for a new indoor space; return to base on low battery; " +
          "flagging change against a baseline image; anomaly detection during the flight " +
          "rather than after it.",
      },
      {
        when: "Thanksgiving · 23–29 Nov",
        status: "Next",
        title: "A lighter week",
        body: "Hannah and Reagan work ten hours rather than twelve; everyone else keeps their usual hours.",
      },
      {
        when: "Sprint 5 · 30 Nov – 6 Dec",
        status: "Next",
        title: "Remaining stretch goals",
        body:
          "A second, closer pass over a flagged point; a model that combines images, readings " +
          "and the equipment's history; obstacle detection with a range sensor; plain-English " +
          "commands.",
      },
      {
        when: "Final · 7–13 Dec",
        status: "Next",
        title: "Demo and hand-over",
        body: "Final demonstration, the documentation complete, and the system handed over runnable by someone outside the team.",
      },
    ],
    planTitle: "Our project management plan",
    planBody:
      "Everything above comes from this document: the person-hours each of us has, all 41 user " +
      "stories with their owners, estimates and acceptance criteria, the Gantt chart, and the " +
      "Sprint 1 backlog.",
    planLinkLabel: "Read the plan (PDF)",
    planFile: "/docs/dronedeck-project-management-planning.pdf",
    showGallery: true,
    galleryTitle: "From the gallery",
    teamLine:
      "Built by Hannah Alexander, Reagan Gary, Kevin Loi, Yordanos Tessema and Samuel Zih, on a " +
      "Crazyflie 2.1 with Lighthouse positioning.",
    teamLinkLabel: "Meet the team",
  },
  projects: {
    eyebrow: "Six parts",
    title: "How DroneDeck is built",
    intro:
      "The six parts our planning document divides the work into, one page each: what the part " +
      "does, how we built it or plan to, who owns which story, and what is left. The team's " +
      "design document is here too.",
  },
  team: {
    eyebrow: "Team 18",
    title: "The team",
    intro:
      "Five people, one part each to lead: Hannah — autonomous flight; Kevin — data " +
      "collection; Reagan — anomaly detection; Samuel — the apps, manual flight and live view; " +
      "Yordanos — documentation. Testing is shared.",
  },
  gallery: {
    eyebrow: "Gallery",
    title: "The kit, the team, and the drone in the air",
    intro: "Photos and videos from building and flying DroneDeck, grouped into albums.",
  },
  apps: {
    eyebrow: "Apps",
    title: "Get DroneDeck",
    intro:
      "The desktop app is not downloaded: each computer builds and installs its own copy with " +
      "one command, on macOS, Windows or Linux. The dashboard is this website — nothing to install.",
    note:
      "The desktop app has to run on the computer with the Crazyradio plugged in — the radio " +
      "is a USB dongle, so no website can command the drone.",
    unsignedTitle: "Why there is no download",
    unsignedIntro:
      "A capstone project has no Apple or Microsoft code-signing certificate, so a downloaded " +
      "build meets a security warning on every machine — and an Intel Mac and an Apple-silicon " +
      "Mac each need their own. Building on the computer that will run it avoids both: the " +
      "install command checks the computer first and says exactly what to install.",
  },
  "setup-index": {
    eyebrow: "Set Up",
    title: "Set up the system",
    intro:
      "Two pieces: the desktop app that flies the drone, and the dashboard that reads what it " +
      "records. Start with the desktop app — it is the only one you need to fly.",
  },
  "setup-web": {
    title: "Run the dashboard",
    intro:
      "Nothing to install to use it — sign in and open the dashboard. This is for running the "
      + "site locally, or deploying your own copy.",
    steps: [
      {
        title: "Install and start it",
        paragraphs: [
          "Node 24 and pnpm. The site talks to the same Supabase project as the deployed one, so "
          + "what you see locally is real data.",
        ],
        bullets: ["cd web", "pnpm install", "pnpm dev"],
        note: "",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Point it at Supabase",
        paragraphs: [
          "Three variables in web/.env.local. The build succeeds without them — the operator "
          + "area degrades rather than the site blanking — but sign-in will not work.",
        ],
        bullets: [
          "NEXT_PUBLIC_SUPABASE_URL",
          "NEXT_PUBLIC_SUPABASE_ANON_KEY",
          "NEXT_PUBLIC_SITE_URL",
        ],
        note: "Never put the service-role key in this file. It bypasses row-level security, and "
              + "anything in a NEXT_PUBLIC_ variable is shipped to the browser.",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Deploy it",
        paragraphs: [
          "Vercel, with the root directory set to web. Left unset, Vercel scans the repository "
          + "root, finds the agent's pyproject.toml, decides this is a FastAPI project and fails "
          + "the build asking for an entrypoint. Never give it one: the agent talks to a USB "
          + "radio and cannot run in the cloud.",
        ],
        bullets: [
          "Root Directory: web",
          "The same three environment variables, in the Vercel project",
          "Pushes to main deploy on their own",
        ],
        note: "",
        showDownloads: false,
        linkHardware: false,
      },
    ],
    troubleTitle: "When it will not build",
    troubleIntro: "Both of these have caught someone on this project.",
    troubles: [
      {
        symptom: "Vercel asks for a FastAPI entrypoint",
        cause: "The root directory is not set to web, so it detected the flight agent.",
        fix: "Set Root Directory to web in the Vercel project settings. .vercelignore is a "
             + "second line of defence, not the fix.",
      },
      {
        symptom: "A page says the table could not be found",
        cause: "A migration was applied by hand and PostgREST is still holding its old schema.",
        fix: "Run notify pgrst, 'reload schema'; against the database. Until then new tables "
             + "answer 404 PGRST205 even though they exist.",
      },
    ],
  },
  "setup-desktop": {
    title: "Set up the system",
    intro: "From an unopened box to a first flight. About 30 minutes, most of it waiting for a battery.",
    steps: [
      {
        title: "Check you have everything",
        paragraphs: ["Six things. Miss one and the drone either will not fly or will not know where it is."],
        bullets: [
          "Crazyflie 2.1 drone, assembled with propellers fitted",
          "Lighthouse positioning deck — a small board with four black domes",
          "Two Lighthouse V2 base stations",
          "Crazyradio PA — the USB dongle with the antenna",
          "At least one charged LiPo battery",
          "A USB-C to micro-USB cable, for charging",
        ],
        note: "",
        showDownloads: false,
        linkHardware: true,
      },
      {
        title: "Install from a terminal — Mac",
        paragraphs: [
          "One command builds DroneDeck on your Mac and puts it in Applications — right for "
          + "Apple silicon and Intel alike. The first build takes about ten minutes; updates are "
          + "quicker. You need two things before you start:",
        ],
        needs: [
          "Git — type git --version in Terminal. If macOS offers to install the developer "
            + "tools, accept: that is Git and the Xcode Command Line Tools in one go",
          "Node.js 24 — from nodejs.org",
        ],
        commands: [
          "git clone https://github.com/rbgary/drone-capstone-project.git",
          "cd drone-capstone",
          "node scripts/install.mjs",
        ],
        bullets: [],
        note: "Everything else it checks for itself, first — Xcode Command Line Tools, Rust, "
              + "pnpm 10 and Python 3.11 or newer — and lists each one missing with the exact "
              + "command to install it, all at once. Install those, run node scripts/install.mjs "
              + "again, and it carries on. Running it again later is also how you update.",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Install from a terminal — Windows",
        paragraphs: [
          "One command builds DroneDeck on your PC and installs it for your user — no "
          + "administrator prompt — and it appears in the Start menu. The first build takes about "
          + "ten minutes. Use PowerShell, Command Prompt or Git Bash, in a normal Windows folder. "
          + "You need two things before you start:",
        ],
        needs: [
          "Git for Windows — from git-scm.com",
          "Node.js 24 — from nodejs.org",
        ],
        commands: [
          "git clone https://github.com/rbgary/drone-capstone-project.git",
          "cd drone-capstone",
          "node scripts/install.mjs",
        ],
        bullets: [],
        note: "Everything else it checks for itself, first — Microsoft C++ Build Tools (with "
              + "\"Desktop development with C++\" ticked), Rust, pnpm 10 and Python 3.11 or newer "
              + "(tick \"Add python.exe to PATH\" when you install it) — and lists each one "
              + "missing with its fix, all at once. The C++ Build Tools are the slow one, so "
              + "installing them before you start saves a round trip. Then install the radio's "
              + "driver — see Plug in the radio.",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Install from a terminal — Linux",
        paragraphs: [
          "One command builds DroneDeck and installs it for your user — no sudo for the "
          + "install itself — in your applications menu and as the command dronedeck. The "
          + "first build takes about ten minutes. Ubuntu under WSL on a Windows PC works the same "
          + "way; the radio needs one extra step there (see Plug in the radio). You need two "
          + "things before you start:",
        ],
        needs: [
          "Git",
          "Node.js 24 — from nodejs.org",
        ],
        commands: [
          "git clone https://github.com/rbgary/drone-capstone-project.git",
          "cd drone-capstone",
          "node scripts/install.mjs",
        ],
        bullets: [],
        note: "Everything else it checks for itself, first — Rust, pnpm 10, Python 3.11 or newer "
              + "with venv and its shared library, and Tauri's build libraries — and prints one "
              + "install command for your distribution. On Debian or Ubuntu it is: sudo apt "
              + "install libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev "
              + "libayatana-appindicator3-dev librsvg2-dev libdbus-1-dev pkg-config xdg-utils "
              + "python3 python3-venv python3-dev. Then give yourself access to the radio — see "
              + "the next step.",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Plug in the radio",
        paragraphs: [
          "The Crazyradio goes into your laptop, not the drone. It is a USB-A plug, so most " +
          "modern Macs need a USB-C adapter or a hub. On a Mac there is no driver to install — " +
          "the app finds it.",
          "On Windows the radio needs a driver, installed once. The app tells you when it is " +
          "missing:",
        ],
        bullets: [
          "Download Zadig from zadig.akeo.ie and run it",
          "Choose Options → List All Devices, then pick Crazyradio PA USB Dongle",
          "Pick libusb-win32 as the driver and click Install Driver (Replace Driver if it " +
            "already has another one)",
        ],
        note: "It must be libusb-win32, not WinUSB — the flight library talks to the radio " +
              "through libusb-win32 on Windows. On Linux, only root may open the radio until " +
              "you add Bitcraze's udev rule and join the plugdev group, once, then log out and " +
              "back in. The install command prints the exact commands, and the app says so " +
              "if it finds the radio but may not open it. On Ubuntu under WSL, first attach " +
              "the dongle to WSL with usbipd-win (learn.microsoft.com/windows/wsl/connect-usb) " +
              "— until then WSL cannot see any USB device — then add the udev rule as above.",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Set up positioning",
        paragraphs: [
          "The drone cannot hold a position without this. Two base stations, in opposite corners " +
          "of the flight area, about two metres up, both angled toward the middle. One works; " +
          "two is steadier, because a single station leaves nothing to fall back on when the " +
          "drone blocks its own view.",
          "Then measure where the stations are, once: quit the app, and from the drone-capstone " +
          "folder run ./dronedeck geometry --distance 1.0 — it asks you to move the drone " +
          "between samples. Repeat it any time a base station is moved or knocked.",
        ],
        bullets: [],
        note: "",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Check before you fly",
        paragraphs: ["On the Control page, press Start session. Nothing spins — the app asks the drone its questions and shows each answer as it arrives, among them:"],
        bullets: [
          "Is the battery charged enough to arm?",
          "Is the positioning deck fitted?",
          "Can it see the base stations?",
          "Has its position estimate settled?",
        ],
        note: "Green means ready. Anything else names what to fix, in plain words — and flying without positioning needs a second, explicit confirmation.",
        showDownloads: false,
        linkHardware: false,
      },
      {
        title: "Your first flight",
        paragraphs: [
          "Start small: 30 cm for a few seconds. Put the drone on the floor, not a table, with " +
          "about two metres clear around it and nothing fragile nearby.",
          "It will take off, hold its height, and land by itself. Once that works, scale up.",
        ],
        bullets: [],
        note: "",
        showDownloads: false,
        linkHardware: false,
      },
    ],
    commandsTitle: "Useful commands",
    commandsIntro:
      "Run these in a terminal, inside the drone-capstone folder you cloned. Each one works on " +
      "every system unless it names one.",
    reference: [
      { group: "Install and update", command: "git pull",
        what: "Get the newest version of the code." },
      { group: "Install and update", command: "node scripts/install.mjs",
        what: "Install DroneDeck, or update it to what you just pulled. Quit the app first — " +
              "the command will not replace a copy that is running, because closing it lands a " +
              "drone that is flying." },
      { group: "Install and update", command: "node scripts/install.mjs --open",
        what: "The same, then open the app." },
      { group: "Install and update", command: "node scripts/install.mjs --no-install",
        what: "Build only, and say where the build is. The installed app is left alone." },
      { group: "Install and update", command: "node scripts/setup.mjs --check",
        what: "Check this computer for everything the build needs, and change nothing." },
      { group: "Open and remove", command: "open -a DroneDeck",
        what: "Mac: open the app. It is also in Applications and Launchpad." },
      { group: "Open and remove", command: "dronedeck",
        what: "Linux: open the app, when ~/.local/bin is on your PATH. It is also in your " +
              "applications menu. On Windows, open it from the Start menu." },
      { group: "Open and remove", command: "rm -rf /Applications/DroneDeck.app",
        what: "Mac: remove the app — or drag it from Applications to the Bin. If it was " +
              "installed to ~/Applications, remove it from there." },
      { group: "Open and remove", command: "& \"$env:LOCALAPPDATA\\DroneDeck\\uninstall.exe\"",
        what: "Windows, in PowerShell: remove the app. Or Settings → Apps → Installed apps → " +
              "DroneDeck → Uninstall." },
      { group: "Open and remove",
        command: "rm -r ~/.local/lib/dronedeck ~/.local/share/applications/dronedeck.desktop " +
                 "~/.local/share/icons/hicolor/128x128/apps/dronedeck.png ~/.local/bin/dronedeck",
        what: "Linux: remove the app. The install command prints this line with your exact paths." },
      { group: "Logs", command: "open ~/Library/Application\\ Support/DroneDeck/logs",
        what: "Mac: the folder with agent.log — send it to us when the app reports a problem. " +
              "When the app was renamed from CropWatcher it moved this folder across, so " +
              "older sessions are still found." },
      { group: "Logs", command: "explorer \"$env:APPDATA\\DroneDeck\\logs\"",
        what: "Windows, in PowerShell: the same folder." },
      { group: "Logs", command: "ls ~/.local/share/DroneDeck/logs",
        what: "Linux: the same folder." },
      { group: "Flight lab (developers)", command: "./dronedeck check",
        what: "Every pre-flight check, from the terminal. Never spins a motor. Quit the app " +
              "first — only one program can use the radio at a time. The ./ matters: it runs the " +
              "flight agent from the repo folder. On Windows, run these in Git Bash." },
      { group: "Flight lab (developers)", command: "./dronedeck stations --seconds 20",
        what: "Which base stations the drone is receiving, live, for 20 seconds." },
      { group: "Flight lab (developers)", command: "./dronedeck geometry --distance 1.0",
        what: "Measure where the base stations are. It asks you to move the drone between " +
              "samples. Run it again whenever a station is moved." },
      { group: "Flight lab (developers)", command: "./dronedeck hover --height 0.3 --secs 10",
        what: "Take off to 30 cm above the floor, hold for 10 seconds, land. Clear space first." },
      { group: "Flight lab (developers)", command: "./dronedeck proptest",
        what: "The firmware's propeller test. The propellers spin — keep hands clear." },
    ],
    troubleTitle: "When it will not fly",
    troubleIntro:
      "Every one of these cost us real time during bring-up. Symptoms first, because that is " +
      "what you actually see.",
    troubles: [
      {
        symptom: "I press fly and absolutely nothing happens",
        cause: "The battery is too low for the drone to arm.",
        fix:
          "The drone refuses to spin its motors below about 3.75 V, and it will not tell you out " +
          "loud. Charge for 20–30 minutes, then power-cycle the drone. The Check screen shows this " +
          "as 'firmware will not arm'.",
      },
      {
        symptom: "The app says no drone found",
        cause: "The radio or the drone is not visible.",
        fix:
          "Confirm the dongle is plugged in — with an adapter if your laptop is USB-C only. Then " +
          "confirm the battery is connected and the drone is switched on. LEDs lighting up does " +
          "not mean the battery is charged.",
      },
      {
        symptom: "Windows: the app says the Crazyradio has no driver, or the wrong one",
        cause: "Windows does not install the radio's driver by itself.",
        fix:
          "Run Zadig, choose Options → List All Devices, pick Crazyradio PA USB Dongle, choose " +
          "libusb-win32 and click Install Driver — or Replace Driver if it shows WinUSB. Then " +
          "unplug the dongle and plug it back in.",
      },
      {
        symptom: "Ubuntu under WSL: the app opens, but finds no Crazyradio",
        cause: "WSL cannot see USB devices until one is attached to it from Windows.",
        fix:
          "Install usbipd-win on Windows and attach the dongle to WSL, as Microsoft describes at " +
          "learn.microsoft.com/windows/wsl/connect-usb, then add the udev rule (the install " +
          "command prints it). Or install DroneDeck on Windows itself, from PowerShell — then " +
          "it is in the Start menu and uses the Zadig driver instead.",
      },
      {
        symptom: "Linux: pnpm app fails with \"xdg-open binary not found\"",
        cause: "pnpm app is the developer build; it also makes an AppImage, which needs xdg-open.",
        fix:
          "Install the xdg-utils package (on Debian or Ubuntu: sudo apt install xdg-utils) and run " +
          "it again. node scripts/install.mjs does not need it — it builds only what it installs.",
      },
      {
        symptom: "The old CropWatcher app is still installed",
        cause: "The app was renamed DroneDeck on 28 September 2026.",
        fix:
          "Run node scripts/install.mjs again after git pull. Once DroneDeck is installed it " +
          "removes the CropWatcher copy it installed before — quit that copy first. Your sign-in, " +
          "sessions and saved Wi-Fi passwords carry over.",
      },
      {
        symptom: "Linux: the app says this user may not open the Crazyradio",
        cause: "Without a udev rule, Linux lets only root use the dongle.",
        fix:
          "Run node scripts/install.mjs again: it checks for the rule and prints Bitcraze's exact " +
          "commands (a rule in /etc/udev/rules.d and the plugdev group). Log out and back in " +
          "afterwards, then unplug the dongle and plug it back in.",
      },
      {
        symptom: "The app says it is not connected to the flight agent",
        cause: "The part of the app that talks to the radio stopped, or another copy holds its port.",
        fix:
          "The message says which. Quit every copy of DroneDeck (and any agent started from a " +
          "terminal), then reopen it. If it stops again, its log is in ~/Library/Application " +
          "Support/DroneDeck/logs on a Mac, %APPDATA%\\DroneDeck\\logs on Windows and " +
          "~/.local/share/DroneDeck/logs on Linux — send us agent.log.",
      },
      {
        symptom: "It lifts a few centimetres and will not go higher",
        cause: "It has no position feedback, so it cannot climb safely.",
        fix:
          "Check the Lighthouse deck is fitted and both base stations are powered and visible. If " +
          "the Check screen shows zero base stations, something is blocking the line of sight.",
      },
      {
        symptom: "It drifts around instead of holding still",
        cause: "The position estimate is unstable where the drone is sitting.",
        fix:
          "Move it back toward the middle of the flight area and try again. If it keeps drifting, " +
          "re-run geometry calibration — a base station has probably been moved.",
      },
      {
        symptom: "It flew, then landed on its own part-way through",
        cause: "The battery dropped to the safety threshold.",
        fix:
          "This is the system working. It lands under control rather than cutting out mid-air. " +
          "Charge fully before a long mission — a full pack gives roughly four minutes of flight.",
      },
      {
        symptom: "Manual control will not connect",
        cause: "The browser cannot reach the app on your machine.",
        fix:
          "Manual flight needs the DroneDeck app running on the same computer or the same " +
          "network. It deliberately does not work over the internet: the delay would make the " +
          "drone unflyable.",
      },
    ],
  },
  hardware: {
    title: "Hardware",
    intro:
      "Every part of the kit, photographed. If you are trying to work out which thing is which, " +
      "this is the page.",
    sensorsTitle: "Where are the sensors?",
    sensorsParagraphs: [
      "This is the most common question, and the answer surprises people: you cannot see most of " +
      "them. The temperature and pressure sensor and the motion sensors are chips two or three " +
      "millimetres across, soldered onto the mainboard. There is no separate “sensor module” to " +
      "find. The only sensor you can point at is the camera on the AI deck.",
      "The Lighthouse positioning deck — a small board with four black domes — is the one part " +
      "that is genuinely easy to miss, and nothing can hold a position without it.",
    ],
    parts: [
      {
        image: "/hardware/02-crazyflie-2.1-mainboard-top-flat.jpeg",
        alt: "Crazyflie 2.1 mainboard seen from above, propellers fitted",
        name: "Crazyflie 2.1 — the drone",
        body:
          "The whole aircraft. Look for the ON/OFF button, the micro-USB port, and the M1–M4 motor " +
          "labels. The white two-pin plug is where the battery goes.",
      },
      {
        image: "/hardware/05-crazyradio-pa-usb-dongle.jpeg",
        alt: "Crazyradio PA USB dongle with its screw-on antenna",
        name: "Crazyradio PA — the radio link",
        body:
          "This plugs into your laptop, not the drone. It is how the two talk. USB-A, so a USB-C " +
          "laptop needs an adapter. No driver to install.",
      },
      {
        image: "/hardware/06-lipo-batteries-x2-and-usb-charger.jpeg",
        alt: "Two LiPo battery packs and a small USB charger board",
        name: "Batteries and charger",
        body:
          "Two 240 mAh packs and the micro-USB charger board. A full pack gives roughly four " +
          "minutes of flight. They ship part-charged, so charge before your first session.",
      },
      {
        image: "/hardware/03-ai-deck-1.1-mounted-underside-a.jpeg",
        alt: "AI deck mounted under the drone, camera module visible at the left edge",
        name: "AI deck — camera (optional)",
        body:
          "The black module at the left edge is the camera. It is the one part that never reached " +
          "a working state: its Wi-Fi datalink was cut from the original project, and nothing here " +
          "depends on it.",
      },
      {
        image: "/hardware/07-cf-battery-holder-and-pin-headers.jpeg",
        alt: "Battery holder circuit board beside two strips of pin headers",
        name: "Battery holder and headers",
        body:
          "The frame that clamps the battery to the drone, plus the long pin headers used to stack " +
          "expansion decks above the mainboard.",
      },
      {
        image: "/hardware/08-spare-motor-mounts-x2.jpeg",
        alt: "Two clear plastic motor mounts",
        name: "Spare motor mounts",
        body:
          "The clear plastic clips that hold a motor at the end of each arm. Replace a cracked one " +
          "before flying — small cracks cause vibration.",
      },
      {
        image: "/hardware/09-spare-coreless-motor-a.jpeg",
        alt: "A single small coreless motor with its connector lead",
        name: "Spare motor",
        body: "One replacement coreless motor, ready to plug in.",
      },
    ],
  },
};

// ── normalising ──────────────────────────────────────────────────────────

const MAX_TEXT = 5000;
const MAX_LIST = 60;

function emptyValue(field: FieldSpec): ContentValue {
  switch (field.type) {
    case "boolean": return false;
    case "strings":
    case "items": return [];
    default: return "";
  }
}

/** An empty item for an `items` list — what the admin's "Add" button creates. */
export function emptyItem(fields: FieldSpec[]): ContentObject {
  return Object.fromEntries(fields.map((f) => [f.key, emptyValue(f)]));
}

function normalizeField(field: FieldSpec, value: unknown, fallback: ContentValue | undefined): ContentValue {
  const safeFallback = fallback ?? emptyValue(field);
  switch (field.type) {
    case "text":
    case "textarea":
      return typeof value === "string" ? value.slice(0, MAX_TEXT) : safeFallback;
    case "image":
      // A site-relative path or an https URL; anything else (javascript:, data:)
      // is refused rather than rendered.
      return typeof value === "string" && (value === "" || /^(\/(?!\/)|https:\/\/)/.test(value))
        ? value.slice(0, MAX_TEXT)
        : safeFallback;
    case "boolean":
      return typeof value === "boolean" ? value : safeFallback;
    case "strings":
      return Array.isArray(value)
        ? value.filter((v): v is string => typeof v === "string").slice(0, MAX_LIST).map((v) => v.slice(0, MAX_TEXT))
        : safeFallback;
    case "items": {
      if (!Array.isArray(value)) return safeFallback;
      return value
        .filter((v): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v))
        .slice(0, MAX_LIST)
        .map((item) => normalizeObject(field.fields, item, emptyItem(field.fields)));
    }
  }
}

function normalizeObject(fields: FieldSpec[], value: Record<string, unknown>, defaults: ContentObject): ContentObject {
  return Object.fromEntries(fields.map((f) => [f.key, normalizeField(f, value[f.key], defaults[f.key])]));
}

/**
 * The page's content: stored values where they are valid, defaults where a
 * field is missing or the wrong type. Unknown keys are dropped.
 */
export function normalizeContent(key: PageKey, stored: unknown): ContentObject {
  const spec = PAGE_SPECS[key];
  const raw = typeof stored === "object" && stored !== null && !Array.isArray(stored)
    ? (stored as Record<string, unknown>)
    : {};
  return normalizeObject(spec.fields, raw, PAGE_DEFAULTS[key]);
}

// ── typed accessors for the pages ────────────────────────────────────────

export const text = (content: ContentObject, key: string): string =>
  typeof content[key] === "string" ? (content[key] as string) : "";

export const flag = (content: ContentObject, key: string): boolean => content[key] === true;

export const strings = (content: ContentObject, key: string): string[] =>
  Array.isArray(content[key]) ? (content[key] as unknown[]).filter((v): v is string => typeof v === "string") : [];

export const items = (content: ContentObject, key: string): ContentObject[] =>
  Array.isArray(content[key])
    ? (content[key] as unknown[]).filter((v): v is ContentObject => typeof v === "object" && v !== null)
    : [];
