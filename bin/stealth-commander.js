#!/usr/bin/env node
const { spawn, spawnSync } = require("child_process");
const path = require("path");

if (process.platform !== "win32" || process.arch !== "x64") {
  console.error("Error: Stealth Commander requires Windows AMD64 (x64), Node.js 18+, and 64-bit Python 3.11+.");
  process.exit(1);
}

function findPython() {
  const selected = process.env.PYCHARM_HARNESS_PYTHON;
  const candidates = selected ? [selected] : ["python", "python3", "py"];
  const probe = "import platform,struct,sys;sys.exit(0 if sys.version_info >= (3,11) and sys.platform == 'win32' and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64') else 1)";
  for (const command of candidates) {
    const prefix = command === "py" ? ["-3"] : [];
    try {
      const result = spawnSync(command, [...prefix, "-c", probe], { stdio: "ignore", windowsHide: true, timeout: 5000 });
      if (!result.error && result.status === 0) return { command, prefix };
    } catch (_) {
      // Try the next verified interpreter; never echo command-line values.
    }
  }
  console.error("Error: 64-bit Python 3.11+ for Windows AMD64 was not found. Install it or set PYCHARM_HARNESS_PYTHON to a supported python.exe.");
  process.exit(1);
}

const python = findPython();
const script = path.join(__dirname, "..", "skills", "stealth-ghost-delegation", "scripts", "stealth_commander_mcp.py");
const userArgs = process.argv.slice(2);
const effectiveArgs = userArgs.length === 0 && process.stdin.isTTY ? ["setup"] : userArgs.filter((argument) => argument !== "serve");
let child;
try {
  child = spawn(python.command, [...python.prefix, "-B", script, ...effectiveArgs], { stdio: "inherit", windowsHide: true });
} catch (_) {
  console.error("Error: Could not start the verified Python interpreter. Check its installation and access permissions.");
  process.exit(1);
}
child.once("error", (error) => {
  const code = typeof error.code === "string" && /^[A-Z0-9_]+$/.test(error.code) ? error.code : "SPAWN_FAILED";
  console.error(`Error: Could not start the verified Python interpreter (${code}). Check its installation and access permissions.`);
  process.exit(1);
});
child.once("exit", (code) => process.exit(Number.isInteger(code) ? code : 1));
