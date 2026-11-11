// Server-only memory backend. Credentials never enter the browser bundle.
export function latticeConfigured(env = process.env) {
  return Boolean(String(env.LATTICE_MEMORY_URL || "").trim());
}

// AbortSignal.timeout() 中止时抛的是 TimeoutError，不是 AbortError；
// 只有 AbortController.abort() 才给 AbortError。两者都算超时。
export function latticeBackendTimedOut(error) {
  return error?.name === "TimeoutError" || error?.name === "AbortError";
}

export async function callLatticeBackend(path, { method = "GET", body, timeoutMs, signal } = {}, { env = process.env, fetchImpl = fetch, timeout = 30_000 } = {}) {
  const base = String(env.LATTICE_MEMORY_URL || "").trim().replace(/\/$/, "");
  const token = String(env.LATTICE_MEMORY_TOKEN || "").trim();
  if (!base || !token) throw new Error("lattice_backend_not_configured");
  const response = await fetchImpl(`${base}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs ?? timeout)]) : AbortSignal.timeout(timeoutMs ?? timeout),
  });
  return { ok: response.ok, status: response.status, payload: await response.json() };
}

export async function callLatticeTool(name, args) {
  const result = await callLatticeBackend("/v1/tools/call", { method: "POST", body: { name, arguments: args } });
  if (!result.ok) throw new Error(`lattice_tool_${result.status}`);
  return result.payload.result;
}
