# k6 UI

Run your team's defined k6 load tests from a browser. Select an environment and scenario, adjust virtual users, runner count and timing, then click **Run test**. Watch runner output, stop a run, or open its metrics in Grafana.

The same console works for services inside Kubernetes and targets outside it. Scenarios come from your synced script repositories, so a new script appears without a deployment change. Each environment lists the hosts a run may target; people can change a scenario's target within that list. Credentials stay in the environment's Kubernetes Secret. The browser cannot choose scripts, credentials, Kubernetes resources or hosts outside the list.

## How it works

A FastAPI backend serves a React/TypeScript frontend. Microsoft Entra ID signs people in through authorization code flow with PKCE. Encrypted, expiring cookies work across replicas. Every operation checks Kubernetes RBAC for the signed-in user and groups; optional catalog groups narrow access further.

Before creating a `k6.io/v1alpha1` TestRun, a separate archiver container runs `k6 archive` with the chosen `VUS`, `RAMP`, `DURATION`, `RAMP_DOWN` and target. k6 freezes script options during archiving, so setting them only on runner pods would run the wrong load. The archiver receives a clean process environment and mounts only scripts and temporary storage. Kubernetes tokens, sign-in credentials and session keys are mounted only in the web container. Its loopback archive endpoint is not exposed by the Service or Ingress. Runner pods receive credentials through `envFrom.secretRef`; the server has no Secret-read permission.

The archive, including relative imports, reaches the operator through a ConfigMap. Run IDs become `testid` tags. The cluster's existing k6 platform configuration supplies remote write and target routing. The console saves all runner exit codes and bounded output before deleting the TestRun and its pods. A failed threshold is a failed test. History stays in Kubernetes for seven days by default, survives restarts, and is then removed automatically. Cancelling retains the run record.

## Install

Install [k6-operator](https://github.com/grafana/k6-operator) first, with permission to watch your load-testing namespace. The console bundles with k6 2.3.0 and pins runner and starter images. Configure your existing Prometheus remote-write output and optional k6 hosts map on the cluster, as you would for a command-line TestRun.

```sh
helm upgrade --install k6-ui oci://ghcr.io/magmamoose/charts/k6-ui \
  --namespace k6-ui --create-namespace --values values.yaml
```

Keep the console namespace restricted to platform administrators: its service account can create TestRuns in the namespaces listed under `rbac.namespaces`. The chart creates an access-review ClusterRole, namespaced runner-management Roles, a stable session key, two web replicas, a PodDisruptionBudget and an ingress NetworkPolicy. Add egress policies for your API server, identity provider and script source to fit your CNI.

Register the callback `https://loadtest.example.com/auth/callback` on the Entra application your Kubernetes authentication configuration trusts. Add a federated identity credential with your cluster's service-account issuer, audience `api://AzureADTokenExchange` and subject `system:serviceaccount:k6-ui:k6-ui`. Set `fullnameOverride: k6-ui`, `azure.tenantId`, `azure.clientId`, and `config.publicOrigin` in chart values. If workload identity is unavailable, disable `azure.workloadIdentity.enabled` and point `azure.clientSecret.existingSecret` at a Secret containing `client-secret`.

`config.kubernetesUser` and `config.kubernetesGroups` must match your API server's claim names and prefixes exactly. For example, if your cluster maps the `roles` claim into groups prefixed with `oidc:`, set `config.kubernetesGroups.claim: roles` and `prefix: "oidc:"`. App assignment alone does not grant permission to run tests: the mapped identity needs TestRun read/create/delete permissions in the configured namespace.

### Defined tests and environments

See [examples/catalog.yaml](examples/catalog.yaml) and [examples/scripts/smoke.js](examples/scripts/smoke.js). Put your private catalog under `catalog.content` in chart values. Mount trusted scripts under `/scripts` through `scripts.volume`, or use `extraContainers` and `extraVolumes` for your existing source-sync sidecars, one per repository.

A suite offers every script under `directory` that matches its `include` globs and none of its `exclude` patterns. A script's first comment line becomes its description. A test names one script, for a suite with a single entry point. Scripts that don't exist yet, such as a repository's unmerged work, are left out of the list rather than failing readiness, so give each sync sidecar its own readiness probe.

k6 runs TypeScript when imports name their files. For code written for a bundler, with imports like `./config`, set `bundle: true` and the archiver bundles the script with esbuild first. `k6` modules stay external.

```yaml
fullnameOverride: k6-ui
azure:
  tenantId: "11111111-1111-1111-1111-111111111111"
  clientId: "22222222-2222-2222-2222-222222222222"
config:
  publicOrigin: https://loadtest.example.com
catalog:
  content:
    suites:
      - id: http
        label: HTTP
        directory: shop/current/scenarios
        include: ["*.js", "**/*.js"]
        exclude: ["_*"]
        target: BASE_URL
        env:
          TLS_VERIFY: "true"
    tests:
      - id: checkout
        label: Checkout journey
        script: checkout/current/src/main.ts
        bundle: true
        target: BASE_URL
    environments:
      - name: dev
        label: Development
        namespace: loadtest
        secret: loadtest-dev
        suites: [http]
        tests: [checkout]
        targets:
          http: https://shop.dev.example.com
          checkout: https://checkout.dev.example.com
        allowed_hosts: ["*.dev.example.com"]
        grafana_url: https://grafana.example.com
        defaults:
          vus: 20
          parallelism: 1
          ramp: 30s
          duration: 2m
          ramp_down: 20s
        max_vus: 1000
        max_parallelism: 4
scripts:
  volume:
    configMap:
      name: loadtest-scripts
```

`target` names the variable a scenario reads its target from, and `targets` sets each suite's or test's default per environment. A person may replace it with an `http(s)` URL without credentials, query or fragment whose host matches `allowed_hosts`. The chosen target also overrides the same key in the Secret. Provision `loadtest-dev` in the `loadtest` namespace with the credentials your scripts read. An external test environment uses another Secret, and its hosts must be reachable from the runner network. Credentials stay out of the catalog and archives. Supply non-secret flags that affect frozen options, such as TLS validation, in a test's `env` map. Do not use remote imports carrying authentication in their URLs.

Environment defaults make a defined test a single click. Limits apply on the server as well as in the browser: virtual users, runner count, positive hold duration and at most one hour of load. Kubernetes quotas remain the cluster-wide load budget. Run output is available to people who may read TestRuns in that environment, so test scripts must never print credentials or personal data.

### Theme

Set `branding.accent`, `accentText`, `accentDark` and `accentTextDark` to your own `"#rrggbb"` colours. The default theme is generic. Branding is supplied by each deployment and requires no public code changes.

### Settings

The chart maps values into `K6_UI_PUBLIC_ORIGIN`, `K6_UI_CATALOG`, `K6_UI_SCRIPTS_DIR`, `K6_UI_SESSION_KEY_FILE`, `K6_UI_SESSION_HOURS` and `K6_UI_RETENTION_HOURS`. Retention accepts 1–720 hours; sessions accept 1–24 hours. Keys must contain at least 32 characters. `K6_UI_USERNAME_CLAIM/PREFIX` and `K6_UI_GROUPS_CLAIM/PREFIX` configure the Kubernetes identity mapping. `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_FEDERATED_TOKEN_FILE` configure sign-in. Private CA certificates for Kubernetes come from the projected service-account volume.

## Development

```sh
uv sync --locked
uv run pytest -q
uv run python -m k6_ui check examples/catalog.yaml
cd frontend
npm ci --ignore-scripts
npm run build
```

`docker buildx bake app-local` builds one local image used by the web and archiver containers. The Helm chart separates their mounts. For local Python development, run `python -m k6_ui.archiver` alongside the web server with `K6_UI_SCRIPTS_DIR` set to your scripts directory. HTTP public origins are accepted only on loopback during local development. Access logging is disabled so authorization codes never enter web logs.

The release workflow publishes `ghcr.io/magmamoose/k6-ui` and the OCI Helm chart `ghcr.io/magmamoose/charts/k6-ui`. Release versions come from Git tags and appear in the console.

Licensed under Apache-2.0.
