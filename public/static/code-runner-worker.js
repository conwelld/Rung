// A module worker: loaded with new Worker(url, { type: "module" }). The file is
// .js rather than .mjs because every static server maps .js to a JavaScript
// MIME type, and a module worker served as anything else refuses to start.
import { loadPyodide } from "https://cdn.jsdelivr.net/pyodide/v314.0.5/full/pyodide.mjs";

let runtime;

async function boot() {
  try {
    runtime = await loadPyodide();
    self.postMessage({ type: "ready" });
  } catch (error) {
    self.postMessage({ type: "boot-error", error: String(error) });
  }
}

boot();

async function runCode(code) {
  const stdout = [];
  const stderr = [];
  runtime.setStdout({ batched: (text) => stdout.push(text) });
  runtime.setStderr({ batched: (text) => stderr.push(text) });

  let value;
  try {
    value = await runtime.runPythonAsync(String(code || ""));
    let display = "";
    if (value !== undefined && value !== null) display = String(value);
    if (value && typeof value.destroy === "function") value.destroy();
    const chunks = stdout.slice();
    if (display && (!chunks.length || chunks[chunks.length - 1] !== display)) chunks.push(display);
    self.postMessage({ type: "result", output: chunks.join("\n") });
  } catch (error) {
    self.postMessage({
      type: "result",
      error: stderr.concat(String(error)).filter(Boolean).join("\n"),
    });
  }
}

// Runs the problem's checks with rung/checks.py, whose source the page passes
// in. Everything crosses into Python as strings and comes back as one JSON
// string, so no Python object outlives the call.
async function runChecks(data) {
  runtime.setStdout({ batched: () => {} });
  runtime.setStderr({ batched: () => {} });
  try {
    await runtime.runPythonAsync(String(data.harness || ""));
    runtime.globals.set("__rung_code", String(data.code || ""));
    runtime.globals.set("__rung_function", String(data.fn || ""));
    runtime.globals.set("__rung_tests", JSON.stringify(data.tests || []));
    const raw = await runtime.runPythonAsync(
      "run_checks_json(__rung_code, __rung_function, __rung_tests)");
    self.postMessage({ type: "checks", result: JSON.parse(String(raw)) });
  } catch (error) {
    self.postMessage({ type: "checks", error: String(error) });
  }
}

self.onmessage = async (event) => {
  if (!runtime) return;
  if (event.data?.type === "run") await runCode(event.data.code);
  else if (event.data?.type === "check") await runChecks(event.data);
};
