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
const requestedArgs = userArgs.filter((argument) => argument !== "--pretty");
const effectiveArgs = requestedArgs.length === 0 && process.stdin.isTTY ? ["setup"] : requestedArgs.filter((argument) => argument !== "serve");
const pretty = ["install", "setup"].includes(effectiveArgs[0]) && (userArgs.includes("--pretty") || (process.stdin.isTTY && process.stdout.isTTY));

function successBanner(command) {
  const glyphs = {
    A: [" ### ", "#   #", "#####", "#   #", "#   #"],
    C: [" ####", "#    ", "#    ", "#    ", " ####"],
    D: ["#### ", "#   #", "#   #", "#   #", "#### "],
    E: ["#####", "#    ", "#### ", "#    ", "#####"],
    H: ["#   #", "#   #", "#####", "#   #", "#   #"],
    L: ["#    ", "#    ", "#    ", "#    ", "#####"],
    M: ["#   #", "## ##", "# # #", "#   #", "#   #"],
    N: ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    O: [" ### ", "#   #", "#   #", "#   #", " ### "],
    R: ["#### ", "#   #", "#### ", "#  # ", "#   #"],
    S: [" ####", "#    ", " ### ", "    #", "#### "],
    T: ["#####", "  #  ", "  #  ", "  #  ", "  #  "]
  };
  const word = (text) => Array.from({ length: 5 }, (_, row) => [...text].map((letter) => glyphs[letter][row]).join(" "));
  const ninja = ["", "     .----.", "    /      \\", "   / .----. \\", "  | / >  < \\ |", "  | \\______/ |", "  |  /====\\  |", "   \\ \\____/ /", "    '------'", "", ""];
  const title = [...word("STEALTH"), "", ...word("COMMANDER")];
  const width = process.stdout.columns || 80;
  const art = width >= 72
    ? title.map((line, row) => ninja[row].padEnd(18) + line).join("\n")
    : ["    .----.", "   / .--. \\", "  | [> <] |", "  |  ==== |", "   '-----'"].join("\n");
  const color = process.stdout.isTTY && !("NO_COLOR" in process.env) && process.env.TERM !== "dumb";
  const heading = command === "install" ? "INSTALL COMPLETE" : "SETUP COMPLETE";
  const details = width >= 72
    ? ["Dashboard: https://stealthcommander.pages.dev/dashboard", "Check readiness before starting a desktop task."]
    : ["stealthcommander.pages.dev", "Check readiness before desktop tasks."];
  const lines = ["", art, "", "STEALTH COMMANDER", heading, "", ...details, ""];
  return (color ? "\u001b[38;5;141m" : "") + lines.join("\n") + (color ? "\u001b[0m" : "");
}

let child;
try {
  child = spawn(python.command, [...python.prefix, "-B", script, ...effectiveArgs], { stdio: pretty ? ["inherit", "pipe", "inherit"] : "inherit", windowsHide: true });
} catch (_) {
  console.error("Error: Could not start the verified Python interpreter. Check its installation and access permissions.");
  process.exit(1);
}
child.once("error", (error) => {
  const code = typeof error.code === "string" && /^[A-Z0-9_]+$/.test(error.code) ? error.code : "SPAWN_FAILED";
  console.error(`Error: Could not start the verified Python interpreter (${code}). Check its installation and access permissions.`);
  process.exit(1);
});
if (pretty) {
  let output = "";
  let passthrough = false;
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    if (passthrough) {
      process.stdout.write(chunk);
    } else if (output.length + chunk.length > 1024 * 1024) {
      // Large or unexpected output stays bounded and remains available for diagnosis.
      process.stdout.write(output + chunk);
      output = "";
      passthrough = true;
    } else {
      output += chunk;
    }
  });
  child.once("close", (code) => {
    let result;
    if (!passthrough) {
      try { result = JSON.parse(output); } catch (_) { /* Preserve unfamiliar output. */ }
      if (code === 0 && result && result.ok === true) {
        process.stdout.write(successBanner(effectiveArgs[0]));
      } else {
        process.stdout.write(output);
      }
    }
    process.exit(code === 0 && result && result.ok === false ? 1 : Number.isInteger(code) ? code : 1);
  });
} else {
  child.once("exit", (code) => process.exit(Number.isInteger(code) ? code : 1));
}
