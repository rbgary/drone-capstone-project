#!/usr/bin/env node
// Install DroneDeck from a terminal, on macOS, Windows or Linux, in one command:
//
//   git clone https://github.com/rbgary/drone-capstone-project.git
//   cd drone-capstone
//   node scripts/install.mjs
//
// It runs three things, and stops at the first that fails, saying why:
//   1. scripts/setup.mjs — checks the toolchain (every problem listed at once,
//      each with its fix) and installs the agent and the app's packages;
//   2. the build — the flight agent is frozen and verified first, then only
//      the bundle this OS installs from (.app on macOS, the installer on
//      Windows; the .dmg is skipped: it needs Finder, and nothing here uses it);
//   3. the install — macOS copies the app into /Applications (~/Applications
//      if that is not writable); Windows runs the installer silently, for
//      this user only, so no administrator prompt; Linux installs for this
//      user only too — the program in ~/.local/lib/dronedeck, a menu entry,
//      an icon, and a `dronedeck` command in ~/.local/bin. No sudo anywhere.
//
// Running it again updates the installed app in place.
//
// The app was called CropWatcher until 2026-09-28. A copy installed under that
// name is removed once DroneDeck is installed (removeLegacyCopy) — only files
// this script put there, and never while that copy is running. Sign-in,
// sessions and saved Wi-Fi passwords are untouched: the agent's data folder,
// the app identifier and the keychain entry keep their original names.
//
//   --open         open the app when it is installed
//   --no-install   build only; leave the installed app alone
//   --dev          also install the agent's test tools (passed to setup)
//
// There is no downloadable installer to fetch: every machine builds its own,
// which is also what makes an Intel Mac and an Apple-silicon Mac each get the
// right one (PyInstaller freezes for the CPU of the machine it runs on).
//
// DRONEDECK_INSTALL_DIR (macOS: the folder for DroneDeck.app; Linux: the
// program folder) installs somewhere else — for testing.
//
// The Crazyradio needs a one-time administrator step on Windows (a driver)
// and Linux (a udev rule). This checks and prints the exact commands; it never
// runs them (scripts/lib/radio-access.mjs).

import { spawn, spawnSync } from "node:child_process";
import {
  accessSync, chmodSync, constants, copyFileSync, existsSync, lstatSync, mkdirSync, readFileSync,
  readdirSync, readlinkSync, rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { hasProgram, radioAccessAdvice } from "./lib/radio-access.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DESKTOP = join(ROOT, "desktop");
const RELEASE = join(DESKTOP, "src-tauri", "target", "release");
const WINDOWS = process.platform === "win32";
const MAC = process.platform === "darwin";
const LINUX = process.platform === "linux";
const APP = "DroneDeck";
/** The name it was installed under before 2026-09-28. */
const LEGACY_APP = "CropWatcher";

const USAGE = "usage: node scripts/install.mjs [--open] [--no-install] [--dev]";
const flags = process.argv.slice(2);
for (const flag of flags) {
  if (!["--open", "--no-install", "--dev"].includes(flag)) {
    console.error(`unknown option ${flag}\n${USAGE}`);
    process.exit(2);
  }
}
const OPEN = flags.includes("--open");
const INSTALL = !flags.includes("--no-install") && (MAC || WINDOWS || LINUX);
const version = JSON.parse(readFileSync(join(DESKTOP, "src-tauri", "tauri.conf.json"), "utf8")).version;

function fail(message) {
  console.error(`\n  ${message}\n`);
  process.exit(1);
}

/** Run a step the user should watch. Exits with a clear line if it fails. */
function step(label, command, args, { cwd = ROOT, shell = false, env } = {}) {
  console.log(`\n==> ${label}`);
  const result = spawnSync(command, args, {
    cwd, shell, stdio: "inherit", windowsHide: true, env: env ?? process.env,
  });
  if (result.error || result.status !== 0) {
    fail(`FAILED  ${label}` + (result.error ? `: ${result.error.message}` : " — see the output above."));
  }
}

// ── where it installs, and whether a copy is running there ────────────────

function macInstallDir() {
  if (process.env.DRONEDECK_INSTALL_DIR) return resolve(process.env.DRONEDECK_INSTALL_DIR);
  try {
    accessSync("/Applications", constants.W_OK);
    return "/Applications";
  } catch {
    return join(homedir(), "Applications");
  }
}

// Linux, per the XDG Base Directory spec: programs under ~/.local/lib, the
// menu entry and icon under $XDG_DATA_HOME, commands in ~/.local/bin.
const DATA_HOME = process.env.XDG_DATA_HOME || join(homedir(), ".local", "share");
const linuxPathsFor = (name) => ({
  dir: join(homedir(), ".local", "lib", name),
  menu: join(DATA_HOME, "applications", `${name}.desktop`),
  icon: join(DATA_HOME, "icons", "hicolor", "128x128", "apps", `${name}.png`),
  link: join(homedir(), ".local", "bin", name),
});
const linuxPaths = () => ({
  ...linuxPathsFor("dronedeck"),
  ...(process.env.DRONEDECK_INSTALL_DIR ? { dir: resolve(process.env.DRONEDECK_INSTALL_DIR) } : {}),
});
/** Where a CropWatcher-era install lives: the same layout under the old name. */
const legacyLinuxPaths = () => linuxPathsFor("cropwatcher");

/** A pgrep pattern matching exactly this program, whatever its path holds. */
const exactProgram = (path) => `^${path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}( |$)`;

/**
 * Is the copy about to be replaced running? It could be flying a drone, so
 * this refuses rather than killing it — the operator quits it, which lands
 * the drone and ends the session tidily (desktop/src/main.tsx).
 */
function runningCopy(target) {
  if (MAC) {
    const found = spawnSync("pgrep", ["-f", `${target}/Contents/MacOS/`], { encoding: "utf8" });
    return found.status === 0 && found.stdout.trim() !== "";
  }
  if (LINUX) {
    const found = spawnSync("pgrep", ["-f", exactProgram(join(target, "desktop"))],
      { encoding: "utf8" });
    return found.status === 0 && found.stdout.trim() !== "";
  }
  if (WINDOWS) {
    for (const image of [`${APP}.exe`, `${LEGACY_APP}.exe`, "desktop.exe", "dronedeck-agent.exe", "cropwatcher-agent.exe"]) {
      const found = spawnSync("tasklist", ["/FI", `IMAGENAME eq ${image}`, "/NH"],
        { encoding: "utf8", windowsHide: true });
      if ((found.stdout ?? "").toLowerCase().includes(image.toLowerCase())) return true;
    }
  }
  return false;
}

function refuseIfRunning(target, name = APP) {
  if (INSTALL && runningCopy(target)) {
    fail(`${name} is running${target ? ` from ${target}` : ""}. Quit it first — closing it lands the ` +
      "drone and ends the session — then run this again.");
  }
}

const target = MAC ? join(macInstallDir(), `${APP}.app`) : LINUX ? linuxPaths().dir : null;
// The CropWatcher-era copy, removed after the install — so it must not be
// running either. Skipped for a test install into DRONEDECK_INSTALL_DIR.
const MIGRATE = INSTALL && !process.env.DRONEDECK_INSTALL_DIR;
const legacyTarget = !MIGRATE ? null
  : MAC ? join(macInstallDir(), `${LEGACY_APP}.app`) : LINUX ? legacyLinuxPaths().dir : null;
// Before minutes of building, not after.
refuseIfRunning(target);
if (legacyTarget && existsSync(legacyTarget)) refuseIfRunning(legacyTarget, LEGACY_APP);

// ── 1. setup ──────────────────────────────────────────────────────────────

step("check the toolchain and install what the build needs",
  process.execPath, [join(ROOT, "scripts", "setup.mjs"), ...(flags.includes("--dev") ? ["--dev"] : [])],
  { env: { ...process.env, DRONEDECK_SETUP_QUIET_NEXT: "1" } });

// ── 2. build ──────────────────────────────────────────────────────────────

const bundles = MAC ? ["--bundles", "app"] : WINDOWS ? ["--bundles", "nsis"] : ["--no-bundle"];
console.log("\n  The first build takes several minutes: Rust compiles everything once.");
step(`build ${APP} ${version} (the flight agent is frozen and verified first)`,
  "pnpm", ["tauri", "build", ...bundles], { cwd: DESKTOP, shell: WINDOWS });

if (!INSTALL) {
  const built = MAC ? join(RELEASE, "bundle", "macos", `${APP}.app`)
    : WINDOWS ? join(RELEASE, "bundle", "nsis") : join(RELEASE, "desktop");
  console.log(`\nBuilt. It is at:\n  ${built}\n`);
  process.exit(0);
}

// ── 3. install ────────────────────────────────────────────────────────────

refuseIfRunning(target);   // it may have been opened during the build
let installed;

if (MAC) {
  const source = join(RELEASE, "bundle", "macos", `${APP}.app`);
  if (!existsSync(source)) fail(`The build finished but ${source} is missing.`);
  console.log(`\n==> install to ${target}`);
  // Replace, never merge: a file the new build no longer ships must not linger.
  rmSync(target, { recursive: true, force: true });
  // ditto keeps what cp -R can lose: symlinks, extended attributes and the
  // code signature inside the bundle.
  step(`copy ${APP}.app`, "ditto", [source, target]);
  for (const part of ["desktop", "dronedeck-agent"]) {
    if (!existsSync(join(target, "Contents", "MacOS", part))) {
      fail(`Installed, but ${part} is missing from ${target}. Run this again.`);
    }
  }
  installed = target;
} else if (LINUX) {
  installed = installOnLinux();
} else {
  const folder = join(RELEASE, "bundle", "nsis");
  const setups = existsSync(folder)
    ? readdirSync(folder).filter((f) => f.toLowerCase().endsWith("-setup.exe"))
      .map((f) => join(folder, f)).sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs)
    : [];
  if (setups.length === 0) fail(`The build finished but no installer is in ${folder}.`);
  // /S: silent. Tauri's installer defaults to a per-user install, so no
  // administrator prompt; running it again replaces the installed version.
  step(`run ${setups[0].split(/[\\/]/).pop()} silently`, setups[0], ["/S"]);
  const home = join(process.env.LOCALAPPDATA ?? join(homedir(), "AppData", "Local"), APP);
  const exe = existsSync(home)
    ? readdirSync(home).find((f) => f.toLowerCase().endsWith(".exe") && !f.toLowerCase().startsWith("uninstall")
      && !f.toLowerCase().startsWith("dronedeck-agent"))
    : undefined;
  if (!exe) fail(`The installer ran, but ${APP} is not in ${home}. Run the installer by hand: ${setups[0]}`);
  installed = join(home, exe);
}

if (MIGRATE) removeLegacyCopy();

const openFrom = MAC ? "Applications or Launchpad"
  : WINDOWS ? "the Start menu"
  : "your applications menu (DroneDeck), or run: dronedeck";
console.log(`
Installed ${APP} ${version}:
  ${installed}

Open it from ${openFrom}. To update later: git pull, then run this again.
`);

if (LINUX) {
  const { dir, menu, icon, link } = linuxPaths();
  console.log(`To remove it: rm -r "${dir}" "${menu}" "${icon}" "${link}"\n`);
}

const radio = radioAccessAdvice();
if (radio.length) console.log(`${radio.join("\n")}\n`);

if (OPEN) {
  if (MAC) spawnSync("open", [installed]);
  else if (WINDOWS) spawnSync("cmd", ["/c", "start", "", installed], { windowsHide: true });
  else spawn(installed, [], { detached: true, stdio: "ignore" }).unref();
}

// ── Linux ─────────────────────────────────────────────────────────────────

/**
 * Quote a path for a desktop entry's Exec key (Desktop Entry Spec 1.5, "The
 * Exec key"): inside double quotes, `"`, `` ` ``, `$` and `\` are escaped.
 */
function execQuote(path) {
  return `"${path.replace(/(["`$\\])/g, "\\$1")}"`;
}

function installOnLinux() {
  const { dir, menu, icon, link } = linuxPaths();
  // tauri-build copies the sidecar beside the binary, without its triple;
  // the running app looks for it there.
  const parts = ["desktop", "dronedeck-agent"];
  for (const part of parts) {
    if (!existsSync(join(RELEASE, part))) fail(`The build finished but ${join(RELEASE, part)} is missing.`);
  }

  console.log(`\n==> install to ${dir}`);
  // Replace, never merge: a file the new build no longer ships must not linger.
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  for (const part of parts) {
    copyFileSync(join(RELEASE, part), join(dir, part));
    chmodSync(join(dir, part), 0o755);
  }
  for (const part of parts) {
    if (!existsSync(join(dir, part))) fail(`Installed, but ${part} is missing from ${dir}. Run this again.`);
  }

  console.log("==> add it to the applications menu");
  mkdirSync(dirname(icon), { recursive: true });
  copyFileSync(join(DESKTOP, "src-tauri", "icons", "128x128.png"), icon);
  mkdirSync(dirname(menu), { recursive: true });
  writeFileSync(menu, [
    "[Desktop Entry]",
    "Type=Application",
    "Version=1.5",
    `Name=${APP}`,
    "GenericName=Drone inspection",
    "Comment=Fly the Crazyflie inspection drone and review what it recorded",
    `Exec=${execQuote(join(dir, "desktop"))}`,
    `Icon=${icon}`,
    "Terminal=false",
    "Categories=Science;",
    "",
  ].join("\n"));
  // Both optional: menus pick the entry up without them, only later.
  if (hasProgram("desktop-file-validate")) {
    const check = spawnSync("desktop-file-validate", [menu], { encoding: "utf8" });
    if (check.status !== 0) console.log(`  note: desktop-file-validate: ${check.stdout}${check.stderr}`.trim());
  }
  if (hasProgram("update-desktop-database")) {
    spawnSync("update-desktop-database", [dirname(menu)], { stdio: "ignore" });
  }

  // A `dronedeck` command — but never over a file that is not ours.
  mkdirSync(dirname(link), { recursive: true });
  let ours = true;
  try {
    const stat = lstatSync(link);
    ours = stat.isSymbolicLink() && readlinkSync(link) === join(dir, "desktop");
    if (ours) rmSync(link);
  } catch {
    // Nothing there yet.
  }
  if (ours) {
    symlinkSync(join(dir, "desktop"), link);
    const onPath = (process.env.PATH ?? "").split(":").includes(dirname(link));
    if (!onPath) console.log(`  note: ${dirname(link)} is not on PATH; the menu entry still works.`);
  } else {
    console.log(`  note: ${link} already exists and is not ${APP}'s; left alone.`);
  }
  return join(dir, "desktop");
}

// ── the CropWatcher-era install ───────────────────────────────────────────

/**
 * Remove the copy installed under the old name, once DroneDeck is in place.
 * Only what this script itself installed there: an app bundle / program
 * folder of the old name, and on Linux the menu entry, icon and command that
 * point into it. Anything that does not look like ours is left and reported.
 * The running check happened before the build (refuseIfRunning).
 */
function removeLegacyCopy() {
  if (MAC) {
    const legacy = join(macInstallDir(), `${LEGACY_APP}.app`);
    if (!existsSync(join(legacy, "Contents", "MacOS", "cropwatcher-agent"))) return;
    rmSync(legacy, { recursive: true, force: true });
    console.log(`==> removed the old ${LEGACY_APP}.app — this app replaces it`);
  } else if (WINDOWS) {
    const home = join(process.env.LOCALAPPDATA ?? join(homedir(), "AppData", "Local"), LEGACY_APP);
    const uninstaller = join(home, "uninstall.exe");
    if (!existsSync(uninstaller)) return;
    // /S: silent, the same per-user uninstaller Settings → Apps would run.
    const result = spawnSync(uninstaller, ["/S"], { windowsHide: true });
    console.log(result.status === 0
      ? `==> removed the old ${LEGACY_APP} — this app replaces it`
      : `  note: could not remove the old ${LEGACY_APP}; uninstall it from Settings → Apps.`);
  } else if (LINUX) {
    const { dir, menu, icon, link } = legacyLinuxPaths();
    const program = join(dir, "desktop");
    if (!existsSync(program) || !existsSync(join(dir, "cropwatcher-agent"))) return;
    try {
      if (lstatSync(link).isSymbolicLink() && readlinkSync(link) === program) rmSync(link);
    } catch {
      // No command was installed.
    }
    try {
      if (readFileSync(menu, "utf8").includes(execQuote(program))) rmSync(menu);
    } catch {
      // No menu entry.
    }
    rmSync(icon, { force: true });
    rmSync(dir, { recursive: true, force: true });
    console.log(`==> removed the old ${LEGACY_APP} install — this app replaces it`);
  }
}
