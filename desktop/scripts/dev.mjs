// Own Vite, Python revision publishing and the desktop/browser preview.
// No credential reads, global installs, shell-built commands or arbitrary kills.
import { spawn, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

const base = path.resolve(import.meta.dirname, "..");
const repository = path.dirname(base);
const state = path.join(base, ".dev");
const browser = process.argv.includes("--browser");
const workspace = path.join(
  state,
  browser ? "browser-workspace" : "native-workspace",
);
const pipe =
  "\\\\.\\pipe\\combine-dev-" +
  createHash("sha256").update(base.toLowerCase()).digest("hex").slice(0, 20);
const python = path.join(base, "src-tauri/resources/python/python.exe");
const env = {
  ...process.env,
  COMBINE_DEV_TRACE: path.join(state, "logs/rpc.jsonl"),
  COMBINE_DEV_SESSION_ID: randomUUID(),
};
const cargo = path.join(repository, ".build/cargo");
if (existsSync(path.join(cargo, "bin/cargo.exe"))) {
  env.CARGO_HOME = cargo;
  env.RUSTUP_HOME = path.join(repository, ".build/rustup");
  env.PATH = path.join(cargo, "bin") + path.delimiter + env.PATH;
}
const children = new Set();
let controller,
  timer,
  closing = false,
  current,
  lastSignature,
  logEnabled = false;

function log(message) {
  const line = `[Combine dev] ${message}`;
  console.log(line);
  if (logEnabled) appendToolLog(line + "\n");
}
function appendToolLog(text) {
  const file = path.join(state, "logs/tools.log");
  if (existsSync(file) && statSync(file).size > 5 * 1024 * 1024)
    renameSync(file, file + ".previous");
  appendFileSync(file, text);
}
function checked(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: base,
    env,
    windowsHide: true,
    encoding: "utf8",
    timeout: 30000,
    ...options,
  });
  if (result.error || result.status !== 0)
    throw new Error(
      options.failure ||
        `Prerequisite failed: ${path.basename(command)}. See desktop/README.md.`,
    );
  return result.stdout;
}
function preflight() {
  if (process.platform !== "win32")
    throw new Error(
      "This development launcher currently requires Windows 11 x64.",
    );
  for (const relative of [
    "node_modules/vite/bin/vite.js",
    "node_modules/@tauri-apps/cli/tauri.js",
  ])
    if (!existsSync(path.join(base, relative)))
      throw new Error(
        "Frontend dependencies are missing. Run npm.cmd ci in desktop first.",
      );
  if (!existsSync(python))
    throw new Error(
      "The bundled Python runtime is missing. Prepare it using desktop/scripts/build.ps1 first.",
    );
  checked(
    python,
    [
      "-I",
      "-c",
      "import sys; assert sys.version_info[:2] == (3,12); import pypdf, pypdfium2, PIL, rapidocr_onnxruntime",
    ],
    {
      failure:
        "The bundled Python 3.12 document adapters are incomplete. Run desktop/scripts/build.ps1 to prepare them.",
    },
  );
  if (!browser)
    checked("cargo", ["--version"], {
      failure:
        "Rust MSVC is missing. Install the documented build prerequisites or use -Browser for interface preview.",
    });
}
function publish() {
  const reply = JSON.parse(
    checked(python, ["-I", "scripts/dev_runtime.py"], {
      failure:
        "Python update failed. Last valid revision retained. Check desktop/runtime for syntax errors.",
    }),
  );
  if (reply.error)
    throw new Error(
      `Python edit rejected: ${reply.error} at ${reply.file}:${reply.line}. Last valid revision retained.`,
    );
  current = reply;
  if (reply.changed)
    log(
      `Python revision ${reply.revision.slice(0, 12)} ready for NEW requests. Running requests keep their revision.`,
    );
  writeFileSync(
    path.join(state, "session.json"),
    JSON.stringify(
      {
        pid: process.pid,
        mode: browser ? "browser" : "desktop",
        workspace,
        revision: reply.revision,
        logs: path.join(state, "logs"),
      },
      null,
      2,
    ),
  );
}
function sourceSignature(directory = path.join(base, "runtime")) {
  return readdirSync(directory, { withFileTypes: true })
    .sort((a, b) => a.name.localeCompare(b.name))
    .flatMap((entry) => {
      if (entry.name === "__pycache__") return [];
      const file = path.join(directory, entry.name);
      if (entry.isDirectory()) return sourceSignature(file);
      const community =
        path.relative(path.join(base, "runtime"), file).split(path.sep)[0] ===
        "community";
      if (
        !entry.name.endsWith(".py") &&
        !(community && /\.(json|md|txt)$/.test(entry.name))
      )
        return [];
      return [
        file,
        createHash("sha256").update(readFileSync(file)).digest("hex"),
      ];
    })
    .join("|");
}
function start(label, command, args) {
  const child = spawn(command, args, {
    cwd: base,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.add(child);
  for (const stream of [child.stdout, child.stderr]) {
    stream.setEncoding("utf8");
    stream.on("data", (text) => {
      process.stdout.write(text);
      appendToolLog(`[${label}] ${text}`);
    });
  }
  child.on("error", () => {
    log(`${label} could not start.`);
    void shutdown(1);
  });
  child.on("exit", (code) => {
    children.delete(child);
    if (!closing) {
      log(
        `${label} exited (${code ?? "signal"}). Stopping this development session.`,
      );
      void shutdown(code === 0 ? 0 : 1);
    }
  });
  return child;
}
async function freePort(port) {
  const probe = net.createServer();
  await new Promise((resolve, reject) => {
    probe.once("error", () =>
      reject(
        new Error(
          `Port ${port} is in use. Stop the existing preview first; its process will not be killed.`,
        ),
      ),
    );
    probe.listen(port, "127.0.0.1", () => probe.close(resolve));
  });
}
async function waitForVite() {
  for (let attempt = 0; attempt < 120 && !closing; attempt++) {
    try {
      const response = await fetch("http://127.0.0.1:1420", {
        signal: AbortSignal.timeout(700),
      });
      if (response.ok) return;
    } catch {}
    await delay(250);
  }
  throw new Error("The interface development server did not become ready.");
}
async function shutdown(code = 0) {
  if (closing) return;
  closing = true;
  clearInterval(timer);
  log("Saving a local checkpoint and stopping owned development processes...");
  if (current && existsSync(path.join(workspace, "workspace.sqlite3"))) {
    const launcher = path.join(
      base,
      "src-tauri/resources",
      current.directory,
      "launcher.py",
    );
    const checkpoint = spawnSync(
      python,
      ["-I", launcher, "--root", workspace],
      {
        env,
        windowsHide: true,
        input: JSON.stringify({ method: "workspace.checkpoint", params: {} }),
        encoding: "utf8",
        timeout: 8000,
      },
    );
    let saved = false;
    try {
      saved = Boolean(JSON.parse(checkpoint.stdout).result?.saved);
    } catch {}
    if (!saved)
      log(
        "Checkpoint unavailable; interrupted work will recover from persisted events at the next start.",
      );
  }
  for (const child of children) {
    if (child.exitCode === null && child.pid)
      spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
        timeout: 20000,
      });
  }
  controller?.close();
  const session = path.join(state, "session.json");
  if (
    existsSync(session) &&
    JSON.parse(readFileSync(session, "utf8")).pid === process.pid
  )
    unlinkSync(session);
  log(
    "Stopped. Development data is preserved. The installed application was not touched.",
  );
  process.exit(code);
}
async function stopExisting() {
  await new Promise((resolve, reject) => {
    const client = net.connect(pipe);
    client.setTimeout(2000, () =>
      client.destroy(new Error("Development control did not respond.")),
    );
    client.on("connect", () => client.write("stop\n"));
    client.on("data", () => {
      client.end();
      resolve();
    });
    client.on("error", reject);
  });
  console.log("Stop requested. The development session is saving and exiting.");
}
async function main() {
  if (process.argv.includes("--stop")) {
    try {
      await stopExisting();
    } catch (error) {
      if (error.code === "ENOENT" || error.code === "ECONNREFUSED")
        console.log("No development session is running.");
      else throw error;
    }
    return;
  }
  preflight();
  if (process.argv.includes("--check")) {
    console.log(
      "Development prerequisites OK. No application or API request started.",
    );
    return;
  }
  mkdirSync(path.join(state, "logs"), { recursive: true });
  controller = net.createServer((client) => {
    client.setTimeout(2000, () => client.destroy());
    client.once("error", () => {});
    client.once("data", (command) => {
      if (command.toString().trim() === "stop") {
        client.end("stopping\n", () => void shutdown());
      } else client.end("running\n");
    });
  });
  const owned = await new Promise((resolve, reject) => {
    controller.once("error", (error) =>
      error.code === "EADDRINUSE" ? resolve(false) : reject(error),
    );
    controller.listen(pipe, () => resolve(true));
  });
  if (!owned) {
    console.log(
      "Combine development is already running. Use its window or Stop-Combine-Dev.cmd.",
    );
    return;
  }
  logEnabled = true;
  await freePort(1420);
  if (browser) await freePort(1421);
  checked(process.execPath, ["scripts/prepare_assets.mjs"]);
  lastSignature = sourceSignature();
  publish();
  process.on("SIGINT", () => void shutdown());
  process.on("SIGTERM", () => void shutdown());
  timer = setInterval(() => {
    const signal = path.join(state, "stop-request.json");
    if (existsSync(signal)) {
      try {
        if (
          JSON.parse(readFileSync(signal, "utf8")).session ===
          env.COMBINE_DEV_SESSION_ID
        ) {
          void shutdown();
          return;
        }
      } catch {
        /* A partial native write is retried on the next tick. */
      }
    }
    try {
      const signature = sourceSignature();
      if (signature !== lastSignature) {
        lastSignature = signature;
        publish();
      }
    } catch (error) {
      log(error.message);
    }
  }, 1200);
  log(`Separate development workspace: ${workspace}`);
  log(
    `Logs: ${path.join(state, "logs")} (request metadata only; no keys or document contents).`,
  );
  start("vite", process.execPath, [
    "node_modules/vite/bin/vite.js",
    "--host",
    "127.0.0.1",
  ]);
  await waitForVite();
  if (closing) return;
  if (browser) {
    start("bridge", python, [
      "-I",
      "scripts/dev_bridge.py",
      "--root",
      workspace,
    ]);
    log(
      "Browser preview ready: http://127.0.0.1:1420. Native windows and tray need desktop mode.",
    );
  } else {
    start("tauri", process.execPath, [
      "node_modules/@tauri-apps/cli/tauri.js",
      "dev",
      "--config",
      "src-tauri/tauri.dev.conf.json",
    ]);
    log(
      "Building the real desktop window. The first debug compilation can take several minutes.",
    );
    log(
      "UI edits update automatically. Ctrl+Shift+I opens DevTools. Close the dev window or press Ctrl+C here to stop.",
    );
  }
}
main().catch((error) => {
  console.error(`[Combine dev] ${error.message}`);
  if (controller?.listening) void shutdown(1);
  else process.exitCode = 1;
});
