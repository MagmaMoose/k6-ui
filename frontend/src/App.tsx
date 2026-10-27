import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, configure, type Environment, type Me, type Profile, type Run } from "./api";

const terminal = new Set(["passed", "failed", "cancelled"]);
const status = (value: string) => ({ initialization: "Preparing", starting: "Preparing", initialized: "Preparing", created: "Preparing", started: "Running", stopped: "Finishing", finished: "Finishing", cancelling: "Stopping", passed: "Passed", failed: "Failed", cancelled: "Cancelled", error: "Finishing" }[value] ?? value);
const duration = (value: string) => [...value.matchAll(/(\d+)([hms])/g)].reduce((sum, m) => sum + Number(m[1]) * ({ h: 3600, m: 60, s: 1 }[m[2]] ?? 0), 0);
const when = (value: string) => new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

function Logo() {
  return <svg className="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="var(--accent)" /><path d="M7 21l6-12v12l6-12v12l6-12" fill="none" stroke="var(--accent-text)" strokeWidth="2.5" strokeLinejoin="round" /></svg>;
}

function Shape({ profile }: { profile: Profile }) {
  const ramp = duration(profile.ramp), hold = duration(profile.duration), down = duration(profile.ramp_down);
  const total = ramp + hold + down || 1;
  const x1 = 35 + ramp / total * 530, x2 = 35 + (ramp + hold) / total * 530;
  return <figure className="shape"><figcaption><span>Load profile</span><span>{Math.round(total / 60 * 10) / 10} min total · {profile.vus.toLocaleString()} peak users</span></figcaption>
    <svg viewBox="0 0 600 160" role="img" aria-label={`Ramp up ${profile.ramp}, hold ${profile.vus} users for ${profile.duration}, ramp down ${profile.ramp_down}`}>
      <path d="M35 125H565 M35 75H565 M35 25H565" className="grid" />
      <polygon points={`35,125 ${x1},25 ${x2},25 565,125`} fill="var(--accent-soft)" />
      <polyline points={`35,125 ${x1},25 ${x2},25 565,125`} fill="none" stroke="var(--accent)" strokeWidth="3" strokeLinejoin="round" />
      <text x="35" y="149">0</text><text x="565" y="149" textAnchor="end">{Math.round(total)}s</text><text x="35" y="17">{profile.vus} VUs</text>
    </svg>
    <div className="phases"><span>Ramp up <strong>{profile.ramp}</strong></span><span>Hold <strong>{profile.duration}</strong></span><span>Ramp down <strong>{profile.ramp_down}</strong></span></div>
  </figure>;
}

function Console({ me, logout }: { me: Me; logout: () => void }) {
  const [environment, setEnvironment] = useState(me.environments[0]?.name ?? "");
  const env = me.environments.find(e => e.name === environment);
  const [test, setTest] = useState(env?.tests[0]?.id ?? "");
  const [profile, setProfile] = useState<Profile>(env?.defaults ?? { vus: 20, parallelism: 1, ramp: "30s", duration: "2m", ramp_down: "20s" });
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (!environment) return;
    const controller = new AbortController();
    const load = () => { if (!document.hidden) void api.history(environment, controller.signal).then(data => setRuns(data.runs)).catch(err => { if (!controller.signal.aborted) setError(String(err.message)); }); };
    load();
    const timer = window.setInterval(load, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [environment, refresh]);

  useEffect(() => {
    if (!selected) { setRun(null); return; }
    const controller = new AbortController();
    const load = () => { if (!document.hidden) void api.detail(environment, selected, controller.signal).then(setRun).catch(err => { if (!controller.signal.aborted) setError(String(err.message)); }); };
    load();
    const timer = window.setInterval(load, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [environment, selected, refresh]);

  function choose(next: Environment) {
    setEnvironment(next.name); setTest(next.tests[0].id); setProfile(next.defaults);
    setSelected(null); setRun(null); setRuns([]); setError("");
  }

  async function start(event: FormEvent) {
    event.preventDefault();
    if (!env || busy) return;
    setBusy(true); setError("");
    try {
      const started = await api.start(env.name, test, profile);
      setRun(started); setSelected(started.name); setRefresh(n => n + 1);
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }

  async function stop() {
    if (!run || busy) return;
    setBusy(true); setError("");
    try { setRun(await api.stop(environment, run.name)); setRefresh(n => n + 1); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }

  return <div className="shell">
    <header className="topbar"><a className="brand" href="/"><Logo /><span>k6 UI</span></a><div className="who"><span>{me.name}</span><button className="button" onClick={logout}>Sign out</button></div></header>
    <div className="layout">
      <aside className="sidebar"><p className="eyebrow">Environments</p><nav aria-label="Environments">{me.environments.map(e => <button key={e.name} className={`environment ${e.name === environment ? "active" : ""}`} onClick={() => choose(e)} disabled={busy}><span className="dot" />{e.label}</button>)}</nav><div className="sidebar-foot"><span className="muted">k6 UI</span><span className="mono">{me.version}</span><a href="https://github.com/MagmaMoose/k6-ui" target="_blank" rel="noreferrer">Source code</a></div></aside>
      <main className="content"><div className="page-title"><div><p className="eyebrow">{env?.label ?? "Load testing"}</p><h1>Run a load test</h1><p className="muted">Choose a scenario, set the load, and watch the result.</p></div><span className="pill">Powered by k6</span></div>
        {error && <div className="notice error" role="alert">{error}<button className="dismiss" aria-label="Dismiss error" onClick={() => setError("")}>×</button></div>}
        {!env ? <div className="card empty"><h2>No environments available</h2><p>Your work account needs load-testing access. Contact your platform team.</p></div> : <>
          <div className="workspace"><form className="card configure" onSubmit={start}><div className="card-heading"><h2>Test configuration</h2><button type="button" className="text-button" disabled={busy} onClick={() => setProfile(env.defaults)}>Reset defaults</button></div>
            <label>Scenario<select value={test} onChange={e => setTest(e.target.value)} disabled={busy}>{env.tests.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}</select></label>
            <p className="scenario-description muted">{env.tests.find(t => t.id === test)?.description}</p>
            <div className="field-grid"><label>Virtual users<input type="number" min="1" max={env.max_vus} value={profile.vus} disabled={busy} required onChange={e => setProfile({ ...profile, vus: Number(e.target.value) })} /><small>Maximum {env.max_vus.toLocaleString()} users</small></label>
              <label>Runner pods<input type="number" min="1" max={Math.min(env.max_parallelism, profile.vus)} value={profile.parallelism} disabled={busy} required onChange={e => setProfile({ ...profile, parallelism: Number(e.target.value) })} /><small>Load shared across runners</small></label></div>
            <div className="field-grid timing">{([['ramp', 'Ramp up'], ['duration', 'Hold duration'], ['ramp_down', 'Ramp down']] as const).map(([key, label]) => <label key={key}>{label}<input value={profile[key]} disabled={busy} required pattern="([0-9]+h)?([0-9]+m)?([0-9]+s)?" onChange={e => setProfile({ ...profile, [key]: e.target.value })} /></label>)}</div>
            <Shape profile={profile} /><div className="run-actions"><p className="muted small">Runs against <strong>{env.label}</strong> with its configured credentials.</p><button className="button primary" disabled={busy || !env.can_run}>{busy ? "Working…" : "Run test"}<span aria-hidden="true">▷</span></button></div>
            {!env.can_run && <p className="notice">Your account can view runs. Running tests needs write access.</p>}
          </form>
          <section className="card result" aria-label="Run result"><div className="card-heading"><h2>{run ? "Run result" : "Ready when you are"}</h2>{run && <span className={`badge ${run.status}`}>{status(run.status)}</span>}</div>
            {!run ? <div className="empty result-empty"><svg viewBox="0 0 64 64" width="64" height="64" aria-hidden="true"><circle cx="32" cy="32" r="28" fill="var(--accent-soft)" /><path d="M26 20l18 12-18 12z" fill="var(--accent)" /></svg><h3>Your next test starts here</h3><p className="muted">The run’s progress, output, and results will appear here.</p></div> : <><h3>{run.label}</h3><p className="mono small muted run-name">{run.name}</p><div className="result-stats"><div><span>Virtual users</span><strong>{run.profile.vus.toLocaleString()}</strong></div><div><span>Runners</span><strong>{run.profile.parallelism}</strong></div><div><span>Started</span><strong className="small">{when(run.created)}</strong></div></div>
              <div className="result-links">{run.grafana && <a className="button" href={run.grafana} target="_blank" rel="noreferrer">Open in Grafana ↗</a>}{!terminal.has(run.status) && env.can_run && <button className="button danger" onClick={() => void stop()} disabled={busy}>Stop test</button>}</div>
              {!!run.runners.length && <div className="runners">{run.runners.map(p => <span className="runner" key={p.name}>{p.name.split("-").at(-1)} · {p.exit_code === null ? p.phase : p.exit_code === 0 ? "Passed" : `Exit ${p.exit_code}`}</span>)}</div>}
              <pre className="logs" aria-label="Run output" tabIndex={0}>{run.logs || "Waiting for runner output…"}</pre><p className="muted small">Run by {run.actor}{run.finished ? ` · Finished ${when(run.finished)}` : ""}</p></>}
          </section></div>
          <section className="card history"><div className="card-heading"><h2>Recent runs</h2><span className="muted small">Refreshes every 5 seconds</span></div>{!runs.length ? <div className="empty muted">No tests have run in this environment yet.</div> : <div className="table-wrap"><table><thead><tr><th>Scenario</th><th>Started</th><th>Load</th><th>Result</th><th aria-label="View run" /></tr></thead><tbody>{runs.map(r => <tr key={r.name} className={selected === r.name ? "selected" : ""}><td><button className="text-button" onClick={() => { setRun(null); setSelected(r.name); }}>{r.label}</button><span className="run-id mono">{r.name}</span></td><td>{when(r.created)}</td><td>{r.profile.vus.toLocaleString()} users · {r.profile.duration}</td><td><span className={`badge ${r.status}`}>{status(r.status)}</span></td><td><button className="button small" aria-label={`View ${r.name}`} onClick={() => { setRun(null); setSelected(r.name); }}>View</button></td></tr>)}</tbody></table></div>}</section>
        </>}
      </main>
    </div>
  </div>;
}

export function App() {
  const [me, setMe] = useState<Me | null>(null), [loading, setLoading] = useState(true), [error, setError] = useState("");
  useEffect(() => {
    const auth = new URLSearchParams(location.search).get("auth_error");
    if (auth) { setError(auth); history.replaceState(null, "", "/"); }
    api.me().then(data => { configure(data.csrf, () => setMe(null)); setMe(data); }).catch(err => { if (!(err instanceof ApiError && err.status === 401)) setError(err.message); }).finally(() => setLoading(false));
  }, []);
  async function logout() { try { await api.logout(); setMe(null); } catch (err) { setError((err as Error).message); } }
  if (loading) return <main className="center muted">Loading…</main>;
  if (!me) return <main className="center"><div className="card sign-in"><Logo /><h1>k6 UI</h1><p className="muted">Run your team’s load tests. Track progress and open the results in Grafana.</p>{error && <p className="notice error" role="alert">{error}</p>}<a className="button primary" href="/auth/login">Sign in with Microsoft</a></div></main>;
  return <Console me={me} logout={() => void logout()} />;
}
