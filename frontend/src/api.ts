export type Profile = { vus: number; parallelism: number; ramp: string; duration: string; ramp_down: string };
// target: the default target URL, or null when the scenario takes no target.
export type Test = { id: string; label: string; group: string; description: string; note: string; target: string | null };
export type Environment = { name: string; label: string; tests: Test[]; defaults: Profile; max_vus: number; max_parallelism: number; can_run: boolean; allowed_hosts: string[] };
export type Me = { name: string; csrf: string; version: string; release_notes: string | null; environments: Environment[] };
export type Run = { name: string; environment: string; test: string; label: string; target?: string; created: string; finished?: string; actor: string; status: string; profile: Profile; archive_sha256: string; grafana?: string; logs?: string; runners: { name: string; phase: string; exit_code: number | null }[] };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

let csrf = "";
let signedOut = () => {};
export function configure(token: string, onSignedOut: () => void) { csrf = token; signedOut = onSignedOut; }

async function request<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { method, signal, credentials: "same-origin", headers: method === "GET" ? {} : { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) signedOut();
    throw new ApiError(response.status, data.message ?? `Request failed (${response.status}).`);
  }
  return data as T;
}
const base = (env: string) => `/api/environments/${encodeURIComponent(env)}/runs`;
export const api = {
  me: () => request<Me>("/api/me"),
  logout: () => request("/auth/logout", "POST"),
  history: (env: string, signal?: AbortSignal) => request<{ runs: Run[] }>(base(env), "GET", undefined, signal),
  detail: (env: string, name: string, signal?: AbortSignal) => request<Run>(`${base(env)}/${encodeURIComponent(name)}`, "GET", undefined, signal),
  start: (env: string, test: string, profile: Profile, target: string) => request<Run>(base(env), "POST", { test, profile, target }),
  stop: (env: string, name: string) => request<Run>(`${base(env)}/${encodeURIComponent(name)}/stop`, "POST"),
};
