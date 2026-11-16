#!/usr/bin/env node
// Everything a fresh clone needs before `pnpm app`, on macOS, Windows or Linux
// (inside WSL too — checkWsl says what the radio needs there):
//
//   node scripts/setup.mjs            check, then install
//   node scripts/setup.mjs --check    check only; change nothing
//   node scripts/setup.mjs --dev      also the agent's test and lint tools
//
// To go all the way to an installed app, run scripts/install.mjs instead — it
// runs this first.
//
// One script for every OS, in Node, because Node is the one thing every
// machine building the desktop app already runs (pnpm needs it). A bash
// script and a PowerShell twin would drift; this cannot.
//
// Every check runs before anything is installed, and every problem is listed
// at once with its fix, so a new machine is fixed in one pass rather than one
// error per run. CI runs it, through scripts/install.mjs, on an Apple-silicon
// Mac, an Intel Mac and Windows (.github/workflows/desktop-install.yml).
//
// It checks, rather than assumes, three things that have each broken a build:
//   - which Python actually runs. Windows' `python` can be a Microsoft Store
//     alias that runs nothing; candidates are executed, not trusted.
//   - that Python's CPU matches Rust's target. PyInstaller freezes for the
//     Python's CPU and Tauri builds for Rust's; an x86_64 Python under Rosetta
//     on an Apple-silicon Mac would ship an Intel agent inside an arm64 app.
//   - that an existing venv still runs here. One copied from another machine
//     (another CPU, a Python that is not installed here) is recreated.

import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { radioAccessAdvice } from "./lib/radio-access.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const AGENT = join(ROOT, "backend", "agent");
const DESKTOP = join(ROOT, "desktop");
const VENV = join(AGENT, ".venv");
const WINDOWS = process.platform === "win32";
const MAC = process.platform === "darwin";
const LINUX = process.platform === "linux";
const EXE = WINDOWS ? ".exe" : "";
const VENV_PYTHON = WINDOWS ? join(VENV, "Scripts", "python.exe") : join(VENV, "bin", "python");

const PYTHON_MIN = [3, 11];
const PNPM_MAJOR = 10;
// platform.machine() spellings → the CPU half of a Rust triple.
const CPU = { arm64: "aarch64", aarch64: "aarch64", x86_64: "x86_64", amd64: "x86_64" };

const USAGE = "usage: node scripts/setup.mjs [--check] [--dev]";
const flags = process.argv.slice(2);
for (const flag of flags) {
  if (!["--check", "--dev"].includes(flag)) {
    console.error(`unknown option ${flag}\n${USAGE}`);
    process.exit(2);
  }
}
const CHECK_ONLY = flags.includes("--check");
const DEV = flags.includes("--dev");

// ── output ────────────────────────────────────────────────────────────────

const problems = [];
const ok = (what) => console.log(`  ok   ${what}`);
const note = (what) => console.log(`  --   ${what}`);
const problem = (what, fix) => {
  problems.push({ what, fix });
  console.log(`  NO   ${what}`);
};
const rel = (path) => relative(ROOT, path) || ".";

/** Run a command and capture it. Never throws: a missing program is `ok: false`. */
function run(command, args, { cwd = ROOT, shell = false, timeout = 60_000 } = {}) {
  const result = spawnSync(command, args, { cwd, shell, timeout, encoding: "utf8", windowsHide: true });
  return {
    ok: !result.error && result.status === 0,
    out: (result.stdout ?? "").trim(),
    err: (result.stderr ?? "").trim() || result.error?.message || "",
  };
}

/** Run a command the user should watch (installs). Exits on failure. */
function step(label, command, args, { cwd = ROOT, shell = false } = {}) {
  console.log(`\n==> ${label}`);
  const result = spawnSync(command, args, { cwd, shell, stdio: "inherit", windowsHide: true });
  if (result.error || result.status !== 0) {
    console.error(`\n  FAILED  ${label}` + (result.error ? `: ${result.error.message}` : ""));
    process.exit(1);
  }
}

const versionAtLeast = (have, want) => {
  for (let i = 0; i < want.length; i++) {
    if ((have[i] ?? 0) !== want[i]) return (have[i] ?? 0) > want[i];
  }
  return true;
};

// ── checks ────────────────────────────────────────────────────────────────

/**
 * Linux inside WSL builds, installs and runs (2026-09-27, Ubuntu under
 * Windows, the window through WSLg) — but WSL sees no USB device until one
 * is attached with usbipd-win, so the app finds no Crazyradio. Said, never
 * refused: the app itself works there.
 */
function checkWsl() {
  if (!LINUX) return;
  let release = "";
  try {
    release = readFileSync("/proc/sys/kernel/osrelease", "utf8");
  } catch {
    /* not readable: treat as plain Linux */
  }
  if (!process.env.WSL_DISTRO_NAME && !/microsoft/i.test(release)) return;
  note("This is Linux inside WSL. The app will run, but it cannot see the Crazyradio until "
    + "the dongle is attached to WSL with usbipd-win "
    + "(https://learn.microsoft.com/windows/wsl/connect-usb) — or install on Windows itself, "
    + "from PowerShell.");
}

function checkNode() {
  const [major, minor] = process.versions.node.split(".").map(Number);
  // Vite's own floor (desktop/node_modules/vite/package.json engines).
  const fine = (major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major >= 23;
  if (fine) ok(`Node ${process.versions.node}`);
  else problem(`Node ${process.versions.node} is too old for Vite (needs 20.19+ or 22.12+)`,
    "Install Node 24 LTS from https://nodejs.org");
}

/** Rust's host triple, or null when Rust is missing. */
function checkRust() {
  const onPath = run("rustc", ["-vV"]);
  const fallback = join(homedir(), ".cargo", "bin", `rustc${EXE}`);
  const rustc = onPath.ok ? onPath : existsSync(fallback) ? run(fallback, ["-vV"]) : onPath;
  if (!rustc.ok) {
    problem("Rust is not installed",
      "Install it from https://rustup.rs, then open a new terminal and run this again.");
    return null;
  }
  const triple = /^host:\s*(\S+)/m.exec(rustc.out)?.[1];
  const version = /^rustc\s+(\S+)/m.exec(rustc.out)?.[1] ?? "?";
  if (!triple) {
    problem("Could not read Rust's target from `rustc -vV`", "Reinstall Rust from https://rustup.rs");
    return null;
  }
  if (WINDOWS && !triple.endsWith("-msvc")) {
    problem(`Rust targets ${triple}; Tauri on Windows needs the MSVC toolchain`,
      "Run: rustup default stable-msvc");
    return triple;
  }
  if (!onPath.ok) note(`rustc is not on PATH; using ${fallback}. Add ~/.cargo/bin to PATH.`);
  ok(`Rust ${version} (${triple})`);
  return triple;
}

function checkNativeToolchain() {
  if (MAC) {
    if (run("xcode-select", ["-p"]).ok) ok("Xcode Command Line Tools");
    else problem("Xcode Command Line Tools are not installed", "Run: xcode-select --install");
    return;
  }
  if (WINDOWS) {
    const programs = process.env["ProgramFiles(x86)"] ?? "C:\\Program Files (x86)";
    const vswhere = join(programs, "Microsoft Visual Studio", "Installer", "vswhere.exe");
    const found = existsSync(vswhere) && run(vswhere, [
      "-latest", "-products", "*",
      "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
      "-property", "installationPath",
    ]).out;
    if (found) ok("Microsoft C++ Build Tools");
    else problem("Microsoft C++ Build Tools are not installed (Rust needs its linker)",
      "Install them from https://visualstudio.microsoft.com/visual-cpp-build-tools/ " +
      "and tick \"Desktop development with C++\".");

    // Preinstalled on Windows 10 (1803+) and 11; the installer brings it
    // otherwise. Only `pnpm app` on a stripped-down Windows needs it here.
    const webview = run("reg", ["query",
      "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
      "/v", "pv"]);
    if (webview.ok) ok("Microsoft Edge WebView2");
    else note("WebView2 not found in the registry. If the built app opens blank, install it from " +
      "https://developer.microsoft.com/microsoft-edge/webview2/");
    return;
  }
  if (LINUX) {
    checkLinuxToolchain();
    return;
  }
  note(`${process.platform}: not a system this setup knows. Tauri's prerequisites are at ` +
    "https://v2.tauri.app/start/prerequisites/");
}

// ── Linux ────────────────────────────────────────────────────────────────

/**
 * The packages that provide everything below, per distribution family:
 * Tauri's own lists (https://v2.tauri.app/start/prerequisites/), plus the
 * D-Bus headers (Tauri's window layer, tao, links libdbus — `cargo tree -i
 * libdbus-sys`; until 2026-11-15 the drone Wi-Fi password store did too),
 * pkg-config, and xdg-utils — Tauri's
 * AppImage bundle needs xdg-open, so `pnpm app` failed without it on an
 * Ubuntu that lacked it (2026-09-27). install.mjs never bundles on Linux.
 */
const LINUX_PACKAGES = {
  debian: "sudo apt update && sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget " +
    "file libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev libdbus-1-dev pkg-config xdg-utils",
  fedora: "sudo dnf install webkit2gtk4.1-devel openssl-devel curl wget file " +
    "libappindicator-gtk3-devel librsvg2-devel libxdo-devel dbus-devel pkgconf-pkg-config xdg-utils && " +
    "sudo dnf group install \"c-development\"",
  arch: "sudo pacman -S --needed webkit2gtk-4.1 base-devel curl wget file openssl " +
    "appmenu-gtk-module libappindicator-gtk3 librsvg xdotool dbus xdg-utils",
  suse: "sudo zypper in webkit2gtk3-devel libopenssl-devel curl wget file libappindicator3-1 " +
    "librsvg-devel dbus-1-devel pkg-config xdg-utils && sudo zypper in -t pattern devel_basis",
};

/** "debian" | "fedora" | "arch" | "suse" | null, from /etc/os-release. */
function linuxFamily() {
  let release = "";
  try {
    release = readFileSync("/etc/os-release", "utf8");
  } catch {
    return null;
  }
  const field = (name) => (new RegExp(`^${name}=(.*)$`, "m").exec(release)?.[1] ?? "")
    .replace(/"/g, "").toLowerCase();
  const ids = `${field("ID")} ${field("ID_LIKE")}`.split(/\s+/);
  if (ids.some((id) => ["debian", "ubuntu"].includes(id))) return "debian";
  if (ids.some((id) => ["fedora", "rhel", "centos"].includes(id))) return "fedora";
  if (ids.some((id) => ["arch", "manjaro"].includes(id))) return "arch";
  if (ids.some((id) => id.startsWith("opensuse") || id === "suse" || id === "sles")) return "suse";
  return null;
}

function linuxPackagesFix() {
  const family = linuxFamily();
  return family ? `Run: ${LINUX_PACKAGES[family]}`
    : "Install Tauri's Linux prerequisites (https://v2.tauri.app/start/prerequisites/) " +
      "and the D-Bus development headers.";
}

/**
 * What the Rust half links against, and the tools the build calls. Checked
 * through pkg-config, as the build itself finds them: a missing one fails a
 * Rust build minutes in with a long cargo trace; here it is one line.
 */
function checkLinuxToolchain() {
  const missing = [];
  if (!run("cc", ["--version"]).ok) missing.push("a C compiler (cc)");
  // PyInstaller reads and rewrites binaries with binutils on Linux.
  if (!run("objdump", ["--version"]).ok) missing.push("objdump (binutils)");
  const pkgConfig = run("pkg-config", ["--version"]).ok;
  if (!pkgConfig) missing.push("pkg-config");
  const libraries = ["webkit2gtk-4.1", "javascriptcoregtk-4.1", "libsoup-3.0", "openssl", "dbus-1"];
  if (pkgConfig) {
    for (const library of libraries) {
      if (!run("pkg-config", ["--exists", library]).ok) missing.push(library);
    }
  }
  if (missing.length) {
    problem(`Linux build libraries or tools are missing: ${missing.join(", ")}`, linuxPackagesFix());
  } else {
    ok(`Linux build libraries (${libraries.join(", ")})`);
  }
  // Only `pnpm app` needs it (the AppImage), so it is a note, not a problem.
  if (!run("xdg-open", ["--version"]).ok) {
    note("xdg-open not found. The install does not need it; `pnpm app` does — install the "
      + "xdg-utils package.");
  }
}

function checkPnpm() {
  // pnpm is a .cmd shim on Windows, which Node only launches through a shell.
  const pnpm = run("pnpm", ["--version"], { shell: WINDOWS });
  const major = Number(pnpm.out.split(".")[0]);
  if (!pnpm.ok) problem("pnpm is not installed", `Run: npm install -g pnpm@${PNPM_MAJOR}`);
  else if (major < PNPM_MAJOR) problem(`pnpm ${pnpm.out} is older than ${PNPM_MAJOR}`,
    `Run: npm install -g pnpm@${PNPM_MAJOR}`);
  else ok(`pnpm ${pnpm.out}`);
}

// Two things Debian and Ubuntu ship apart from Python itself, each probed
// rather than discovered minutes into the build:
//   ensurepip        how `python -m venv` puts pip in the new environment
//                    (python3.X-venv); without it venv half-creates a folder
//   libpython3.X.so  what PyInstaller freezes the agent with (libpython3.X);
//                    without it the build stops with "Python shared library
//                    was not found" (found in a Linux container, 2026-09-25).
//                    Asked of the FILE: Ubuntu's Python reports
//                    Py_ENABLE_SHARED=1 whether or not the library is installed.
const PROBE = "import json,os,platform,sys,sysconfig,ctypes.util,importlib.util as u;" +
  "g=sysconfig.get_config_var;v='%d.%d'%sys.version_info[:2];" +
  "print(json.dumps([list(sys.version_info[:3]),platform.machine(),sys.executable," +
  "u.find_spec('ensurepip') is not None,os.path.realpath(sys.executable)," +
  "os.path.exists(os.path.join(g('LIBDIR') or '',g('INSTSONAME') or '-')) or " +
  "ctypes.util.find_library('python'+v) is not None]))";

/** What a Python actually is, by running it. null when it does not run. */
function probePython(command, prefix = []) {
  const result = run(command, [...prefix, "-c", PROBE], { timeout: 30_000 });
  if (!result.ok) return null;
  try {
    const [version, machine, executable, canVenv, real, sharedLib] =
      JSON.parse(result.out.split(/\r?\n/).pop());
    const cpu = CPU[machine.toLowerCase()] ?? machine.toLowerCase();
    return { command, prefix, version, cpu, executable, canVenv, real, sharedLib };
  } catch {
    return null;
  }
}

const pyVersion = (py) => py.version.join(".");

/** A Python ≥ 3.11 for the same CPU Rust builds for. */
function findPython(targetCpu) {
  const candidates = [];
  if (process.env.DRONEDECK_PYTHON) candidates.push([process.env.DRONEDECK_PYTHON]);
  // 3.11 first: it is what CI builds with, so it is the proven one.
  if (WINDOWS) {
    candidates.push(["py", "-3.11"], ["py", "-3.12"], ["py", "-3.13"], ["python"], ["py", "-3"]);
  } else {
    candidates.push(["python3.11"], ["python3.12"], ["python3.13"], ["python3"], ["python"]);
  }

  const rejected = [];
  const seen = new Set();
  for (const [command, ...prefix] of candidates) {
    const py = probePython(command, prefix);
    if (!py) continue;
    // python3 and python3.12 are often the same interpreter: judge it once.
    if (seen.has(py.real)) continue;
    seen.add(py.real);
    if (!versionAtLeast(py.version, PYTHON_MIN)) {
      rejected.push(`${py.executable} is Python ${pyVersion(py)}`);
      continue;
    }
    if (targetCpu && py.cpu !== targetCpu) {
      rejected.push(`${py.executable} is for ${py.cpu}, but Rust builds for ${targetCpu}`);
      continue;
    }
    const [major, minor] = py.version;
    if (!py.canVenv) {
      rejected.push(`${py.executable} cannot make a virtual environment (no ensurepip — on ` +
        `Debian/Ubuntu: sudo apt install python${major}.${minor}-venv)`);
      continue;
    }
    // macOS and Windows builds are framework / DLL builds PyInstaller always
    // finds; only Linux distribution Pythons leave the shared library out.
    if (LINUX && !py.sharedLib) {
      rejected.push(`${py.executable} has no shared library for PyInstaller (libpython` +
        `${major}.${minor}.so — on Debian/Ubuntu: sudo apt install libpython${major}.${minor})`);
      continue;
    }
    ok(`Python ${pyVersion(py)} (${py.cpu}) — ${py.executable}`);
    return py;
  }

  const why = rejected.length ? ` Found, but unusable: ${rejected.join("; ")}.` : "";
  problem(`No Python ${PYTHON_MIN.join(".")}+ ${targetCpu ? `for ${targetCpu} ` : ""}found.${why}`,
    WINDOWS
      ? "Install Python 3.11 from https://www.python.org/downloads/windows/ and tick " +
        "\"Add python.exe to PATH\". (The Microsoft Store `python` alias does not count.)"
      : MAC
        ? "Install Python 3.11 from https://www.python.org/downloads/macos/ (a universal " +
          "installer, right for any Mac), or: brew install python@3.11"
        : "Install Python 3.11 or newer with your package manager — on Debian/Ubuntu: " +
          "sudo apt install python3 python3-venv python3-dev");
  return null;
}

/**
 * Is the existing venv usable HERE? A venv records the absolute path of the
 * Python that made it, so one copied from another machine does not run, and
 * one made by an x86_64 Python on an Apple-silicon Mac freezes the wrong agent.
 */
function venvState(targetCpu) {
  if (!existsSync(VENV)) return { usable: false, why: null };
  const py = probePython(VENV_PYTHON);
  if (!py) return { usable: false, why: "its Python does not run on this machine" };
  if (!versionAtLeast(py.version, PYTHON_MIN)) {
    return { usable: false, why: `it is Python ${pyVersion(py)}; ${PYTHON_MIN.join(".")}+ is needed` };
  }
  if (targetCpu && py.cpu !== targetCpu) {
    return { usable: false, why: `it is for ${py.cpu}, but Rust builds for ${targetCpu}` };
  }
  return { usable: true, why: null, py };
}

// ── main ──────────────────────────────────────────────────────────────────

const os = { darwin: "macOS", win32: "Windows", linux: "Linux" }[process.platform] ?? process.platform;
console.log(`\nDroneDeck setup — ${os} ${process.arch}${CHECK_ONLY ? " (checking only)" : ""}\n`);

checkWsl();
checkNode();
const triple = checkRust();
const targetCpu = triple ? triple.split("-")[0] : null;
checkNativeToolchain();
checkPnpm();
const python = findPython(targetCpu);

const venv = venvState(targetCpu);
if (venv.usable) ok(`agent environment ${rel(VENV)} (Python ${pyVersion(venv.py)})`);
else if (venv.why) note(`agent environment ${rel(VENV)} will be recreated: ${venv.why}`);
else note(`agent environment ${rel(VENV)} will be created`);

if (problems.length) {
  console.log(`\n${problems.length} thing${problems.length === 1 ? "" : "s"} to fix first:\n`);
  for (const { what, fix } of problems) console.log(`  - ${what}\n      ${fix}\n`);
  console.log("Then run this again.\n");
  process.exit(1);
}

if (CHECK_ONLY) {
  console.log("\nEverything needed is here. Run without --check to install.\n");
  process.exit(0);
}

if (!venv.usable) {
  if (existsSync(VENV)) {
    // A venv is disposable by design: everything in it comes from pyproject.
    rmSync(VENV, { recursive: true, force: true });
  }
  step(`create ${rel(VENV)} with Python ${pyVersion(python)}`,
    python.command, [...python.prefix, "-m", "venv", VENV]);
}

step("upgrade pip", VENV_PYTHON,
  ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--upgrade", "pip"]);
step(`install the flight agent${DEV ? " with its dev tools" : ""}`, VENV_PYTHON,
  // --only-binary: cryptography must come as a prebuilt wheel, which carries
  // its own OpenSSL. Compiled here, it links to Homebrew's and the frozen
  // agent cannot import it — an Intel Mac could not sign in (2026-09-25).
  // With this, a missing wheel fails the install instead of the sign-in.
  ["-m", "pip", "install", "--quiet", "--disable-pip-version-check",
   "--only-binary=cryptography",
   "-e", `.[${DEV ? "packaging,dev" : "packaging"}]`],
  { cwd: AGENT });
step("install the desktop app's packages", "pnpm", ["install", "--frozen-lockfile"],
  { cwd: DESKTOP, shell: WINDOWS });

// scripts/install.mjs runs this first and says what comes next itself.
if (process.env.DRONEDECK_SETUP_QUIET_NEXT) process.exit(0);

console.log(`
Ready. To build the app and install it (into Applications on a Mac, for this
user on Windows and Linux):

  node scripts/install.mjs

To build and open it without installing: cd desktop, then pnpm app.
To work on the window with live reload: cd desktop, then pnpm tauri dev.
The first build takes several minutes (Rust compiles everything once).
`);

const radio = radioAccessAdvice();
if (radio.length) console.log(`${radio.join("\n")}\n`);
