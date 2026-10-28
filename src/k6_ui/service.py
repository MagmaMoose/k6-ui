import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from .archive import request_archive
from .config import Config, Environment, Profile, NAME, target_url
from .kube import Kube, KubeError

LABEL = "app.kubernetes.io/created-by=k6-ui,app.kubernetes.io/component=record"
log = logging.getLogger("k6_ui")
TERMINAL = {"passed", "failed", "cancelled"}


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    script: str
    group: str = ""
    description: str = ""
    note: str = ""
    target: str = ""
    env: dict[str, str] = field(default_factory=dict)
    bundle: bool = False


def summary(path: Path) -> str:
    """A script's first comment line, which is how these suites introduce themselves."""
    try:
        head = path.read_bytes()[:4096].decode("utf-8", "replace")
    except OSError:
        return ""
    for line in head.splitlines():
        line = line.strip()
        if line.startswith("//") or line.startswith("*") and not line.startswith("*/"):
            text = line.lstrip("/*").strip()
            if text:
                return text[:200]
        elif line and not line.startswith("/*"):
            return ""
    return ""


class ConsoleError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def core(ns, resource):
    return f"/api/v1/namespaces/{ns}/{resource}"


def runs(ns):
    return f"/apis/k6.io/v1alpha1/namespaces/{ns}/testruns"


class Console:
    def __init__(self, cfg: Config, kube: Kube, bundle=request_archive):
        self.cfg, self.kube, self.bundle = cfg, kube, bundle
        self.archivers = asyncio.Semaphore(2)

    async def allowed(self, actor, env, verb):
        if env.groups and not set(env.groups).intersection(actor.get("groups") or []):
            return False
        review = await self.kube.create("/apis/authorization.k8s.io/v1/subjectaccessreviews", {
            "apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview",
            "spec": {"user": actor["user"], "groups": actor.get("groups", []),
                     "resourceAttributes": {"namespace": env.namespace, "verb": verb, "group": "k6.io", "resource": "testruns"}},
        })
        return review.get("status", {}).get("allowed") is True

    async def environment(self, actor, name, verb="list"):
        env = next((e for e in self.cfg.catalog.environments if e.name == name), None)
        if not env or not await self.allowed(actor, env, verb):
            raise ConsoleError(404, "Environment is unavailable.")
        return env

    def scenarios(self, env: Environment) -> list[Scenario]:
        root = self.cfg.scripts_dir.resolve()
        found = [Scenario(id=t.id, label=t.label, script=t.script, description=t.description, target=t.target, env=t.env, bundle=t.bundle)
                 for t in self.cfg.catalog.tests
                 if t.id in env.tests and (root / t.script).is_file() and (root / t.script).resolve().is_relative_to(root)]
        for suite in (s for s in self.cfg.catalog.suites if s.id in env.suites):
            base = (root / suite.directory).resolve()
            if not base.is_dir() or not base.is_relative_to(root):
                continue
            paths = {p.relative_to(base).as_posix(): p for pattern in suite.include for p in base.glob(pattern)}
            for rel, path in sorted(paths.items()):
                if path.is_file() and path.resolve().is_relative_to(root) and not any(fnmatch(rel, x) for x in suite.exclude):
                    found.append(Scenario(id=f"{suite.id}/{rel}", label=rel.rsplit(".", 1)[0], script=str(PurePosixPath(suite.directory, rel)),
                                          group=suite.label, description=summary(path), note=suite.note, target=suite.target, env=suite.env,
                                          bundle=suite.bundle))
        return found

    def ready(self) -> bool:
        # Scripts may not exist yet (an empty or unmerged source), so they only decide what the catalog lists.
        return self.cfg.scripts_dir.is_dir()

    async def catalog(self, actor):
        found = []
        for e in self.cfg.catalog.environments:
            if await self.allowed(actor, e, "list"):
                found.append({"name": e.name, "label": e.label, "defaults": e.defaults.model_dump(),
                              "max_vus": e.max_vus, "max_parallelism": e.max_parallelism,
                              "can_run": await self.allowed(actor, e, "create"), "allowed_hosts": e.allowed_hosts,
                              "tests": [{"id": t.id, "label": t.label, "group": t.group, "description": t.description, "note": t.note,
                                         "target": next(iter(e.choices(t.id)), "") if t.target else None,
                                         "targets": e.choices(t.id) if t.target else []}
                                        for t in self.scenarios(e)]})
        return found

    async def records(self, ns):
        return await self.kube.list(core(ns, "configmaps"), LABEL)

    def record(self, cm):
        return json.loads(cm["data"]["run.json"])

    async def owned(self, env, name):
        if not re.fullmatch(NAME, name):
            raise ConsoleError(404, "Run is unavailable.")
        cm = await self.kube.get(core(env.namespace, "configmaps") + "/" + name)
        if not cm or cm.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/created-by") != "k6-ui":
            raise ConsoleError(404, "Run is unavailable.")
        if self.record(cm)["environment"] != env.name:
            raise ConsoleError(404, "Run is unavailable.")
        return cm

    async def save(self, ns, cm, record):
        return await self.kube.patch(core(ns, "configmaps") + "/" + cm["metadata"]["name"], {
            "metadata": {"resourceVersion": cm["metadata"]["resourceVersion"]},
            "data": {"run.json": json.dumps(record, separators=(",", ":"))},
        })

    async def start(self, actor, environment, test_id, profile: Profile, target: str = ""):
        env = await self.environment(actor, environment, "create")
        test = next((t for t in self.scenarios(env) if t.id == test_id), None)
        if not test:
            raise ConsoleError(404, "Test is unavailable in this environment.")
        try:
            env.check_profile(profile)
            target = target_url(target.strip() or next(iter(env.choices(test.id)), ""), env.allowed_hosts) if test.target else ""
        except ValueError as err:
            raise ConsoleError(422, str(err)) from err
        chosen = {test.target: target} if target else {}
        async with self.archivers:
            bundle = await self.bundle(self.cfg.scripts_dir, test.script, {**test.env, **chosen, **profile.variables()}, test.bundle)
        now = datetime.now(UTC)
        name = f"k6-ui-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"
        record = {"name": name, "environment": env.name, "test": test.id, "label": test.label, "target": target,
                  "created": now.isoformat(), "actor": actor["user"], "profile": profile.model_dump(),
                  "archive_sha256": hashlib.sha256(bundle).hexdigest(), "status": "starting", "runners": [], "logs": ""}
        if env.grafana_url:
            record["grafana"] = env.grafana_url.rstrip("/") + "/d/k6-load-tests/k6-load-tests?" + urlencode({"var-testid": name, "from": int(now.timestamp() * 1000), "to": "now"})
        cm = await self.kube.create(core(env.namespace, "configmaps"), {
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "labels": {"app.kubernetes.io/created-by": "k6-ui", "app.kubernetes.io/component": "record"}},
            "data": {"run.json": json.dumps(record)},
        })
        spec = {"parallelism": profile.parallelism, "script": {"configMap": {"name": name + "-archive", "file": "archive.tar"}},
                "arguments": f"--tag testid={name}",
                "runner": {"image": "grafana/k6:2.3.0@sha256:9c2dee7f8ed74d317e4027c06a10f169b625638189de8d4555d0b3486a5aeb34",
                           "automountServiceAccountToken": "false",
                           "securityContext": {"runAsNonRoot": True, "runAsUser": 12345, "seccompProfile": {"type": "RuntimeDefault"}},
                           "containerSecurityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                           # Set explicitly, the chosen target wins over the same key in the env Secret.
                           "env": [{"name": "K6_NO_USAGE_REPORT", "value": "true"}, *({"name": k, "value": v} for k, v in chosen.items())]},
                "starter": {"image": "ghcr.io/grafana/k6-operator:starter-v1.6.0@sha256:b97b8e32867a553b7f866acd1e0a2ac91af81480c04c12a6ee55df3ae99d2ad6",
                            "automountServiceAccountToken": "false",
                            "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                            "containerSecurityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}}
        if env.secret:
            spec["runner"]["envFrom"] = [{"secretRef": {"name": env.secret}}]
        try:
            await self.kube.create(core(env.namespace, "configmaps"), {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": name + "-archive", "ownerReferences": [{"apiVersion": "v1", "kind": "ConfigMap", "name": name, "uid": cm["metadata"]["uid"]}]},
                "binaryData": {"archive.tar": base64.b64encode(bundle).decode()},
            })
            await self.kube.create(runs(env.namespace), {"apiVersion": "k6.io/v1alpha1", "kind": "TestRun",
                "metadata": {"name": name, "labels": {"app.kubernetes.io/created-by": "k6-ui"},
                             "ownerReferences": [{"apiVersion": "v1", "kind": "ConfigMap", "name": name, "uid": cm["metadata"]["uid"]}]}, "spec": spec})
        except KubeError:
            record.update(status="failed", finished=now.isoformat(), logs="Kubernetes refused to start the run.")
            await self.save(env.namespace, cm, record)
            raise
        log.info(json.dumps({"msg": "run started", "name": name, "environment": env.name, "test": test.id, "actor": actor["user"]}))
        return record

    async def history(self, actor, environment):
        env = await self.environment(actor, environment)
        found = [self.record(c) for c in await self.records(env.namespace)]
        return sorted([{k: v for k, v in f.items() if k != "logs"} for f in found if f["environment"] == env.name], key=lambda f: f["created"], reverse=True)

    async def detail(self, actor, environment, name):
        env = await self.environment(actor, environment)
        cm = await self.owned(env, name)
        record = self.record(cm)
        if record["status"] not in TERMINAL:
            pods = await self.pods(env.namespace, name)
            record["logs"] = await self.logs(env.namespace, pods)
            record["runners"] = self.runner_states(name, pods)
        return record

    async def stop(self, actor, environment, name):
        env = await self.environment(actor, environment, "delete")
        cm = await self.owned(env, name)
        record = self.record(cm)
        if record["status"] in TERMINAL:
            return record
        record["status"] = "cancelling"
        record["logs"] = await self.logs(env.namespace, await self.pods(env.namespace, name))
        cm = await self.save(env.namespace, cm, record)
        await self.kube.delete(runs(env.namespace) + "/" + name)
        record.update(status="cancelled", finished=datetime.now(UTC).isoformat())
        await self.save(env.namespace, cm, record)
        log.info(json.dumps({"msg": "run cancelled", "name": name, "actor": actor["user"]}))
        return record

    async def pods(self, ns, name):
        pods = await self.kube.list(core(ns, "pods"), "app=k6")
        return [p for p in pods if p.get("metadata", {}).get("labels", {}).get("job-name") in {f"{name}-initializer", f"{name}-starter", *[f"{name}-{i}" for i in range(1, 17)]}]

    def runner_states(self, name, pods):
        found = []
        for p in pods:
            job = p["metadata"]["labels"]["job-name"]
            if job.removeprefix(name + "-").isdigit():
                states = p.get("status", {}).get("containerStatuses", [])
                terminated = next((s["state"]["terminated"] for s in states if "terminated" in s.get("state", {})), None)
                found.append({"name": job, "phase": p.get("status", {}).get("phase", "Pending"), "exit_code": terminated.get("exitCode") if terminated else None})
        return found

    async def logs(self, ns, pods):
        output = []
        for p in sorted(pods, key=lambda p: p["metadata"]["name"]):
            name = p["metadata"]["name"]
            try:
                text = await self.kube.text(core(ns, "pods") + f"/{name}/log", {"tailLines": "120", "limitBytes": "8192"})
            except KubeError as err:
                if err.status not in {400, 404}:
                    raise
                continue
            output.append(f"[{name}]\n{text}")
        return "\n\n".join(output)[:32000]

    async def reconcile(self):
        cutoff = datetime.now(UTC) - timedelta(hours=self.cfg.retention_hours)
        for ns in {e.namespace for e in self.cfg.catalog.environments}:
            for cm in await self.records(ns):
                try:
                    record = self.record(cm)
                    name = record["name"]
                    if record["status"] in TERMINAL:
                        await self.kube.delete(runs(ns) + "/" + name)
                        await self.kube.delete(core(ns, "configmaps") + "/" + name + "-archive")
                        if datetime.fromisoformat(record["finished"]) < cutoff:
                            await self.kube.delete(core(ns, "configmaps") + "/" + name)
                        continue
                    if record["status"] == "cancelling":
                        await self.kube.delete(runs(ns) + "/" + name)
                        record.update(status="cancelled", finished=datetime.now(UTC).isoformat())
                        await self.save(ns, cm, record)
                        continue
                    run = await self.kube.get(runs(ns) + "/" + name)
                    if not run:
                        # A create request may still be between its ConfigMap and TestRun.
                        if datetime.now(UTC) - datetime.fromisoformat(record["created"]) < timedelta(minutes=2):
                            continue
                        record.update(status="failed", finished=datetime.now(UTC).isoformat(), logs="TestRun disappeared before its result was recorded.")
                    else:
                        pods = await self.pods(ns, name)
                        runners = self.runner_states(name, pods)
                        stage = run.get("status", {}).get("stage", "initialization")
                        done = len(runners) == record["profile"]["parallelism"] and all(p["exit_code"] is not None for p in runners)
                        failed = stage == "error" or any(p.get("status", {}).get("phase") == "Failed" and not p["metadata"]["labels"]["job-name"].removeprefix(name + "-").isdigit() for p in pods)
                        expired = datetime.now(UTC) - datetime.fromisoformat(record["created"]) > timedelta(minutes=75)
                        if not (done or failed or expired):
                            if record["status"] != stage:
                                record["status"] = stage
                                await self.save(ns, cm, record)
                            continue
                        record.update(status="passed" if done and not failed and all(p["exit_code"] == 0 for p in runners) else "failed",
                                      finished=datetime.now(UTC).isoformat(), runners=runners, logs=await self.logs(ns, pods))
                        if expired:
                            record["logs"] += "\nRun exceeded the 75-minute execution deadline."
                    await self.save(ns, cm, record)
                    await self.kube.delete(runs(ns) + "/" + name)
                except KubeError as err:
                    if err.status not in {404, 409}:
                        raise

    async def maintain(self):
        while True:
            try:
                await self.reconcile()
            except (KubeError, ValueError, KeyError):
                log.exception("run reconciliation failed")
            await asyncio.sleep(10)
