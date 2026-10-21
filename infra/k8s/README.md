# k8s

Manifests for the `homelab` k3s cluster. Node and k3s setup live in `../ansible` (see `../docs/k3-setup.md`).

| Directory | What goes in it |
|---|---|
| `clusters/homelab/` | Flux itself (`flux-system/`) and the two Flux `Kustomization`s that point it at the directories below |
| `infrastructure/` | Cluster-wide pieces: namespaces, CRDs, operators, ingress, cert-manager |
| `apps/` | Ember workloads, one subdirectory per app, each listed in `apps/kustomization.yaml` |
| `monitoring/` | The collector that sends the cluster's metrics, logs and events to Grafana Cloud ([Monitoring](#monitoring)) |

## How deploys work

The full guide to Flux here (why, layout, secrets, recovery, troubleshooting) is `../docs/flux.md`.

Flux runs on the cluster and follows the `develop` branch (ADR 0003). About once a minute it fetches `infra/k8s`, applies `infrastructure/`, waits until everything there is healthy, then applies `apps/` and `monitoring/`. Those two don't wait for each other, so a broken collector never holds up a deploy. **Merging to `develop` is the deploy.** Nobody runs `kubectl apply` or `helm` against the cluster.

Flux also deletes what is removed from Git (pruning), including the old hashed ConfigMaps a config edit leaves behind. Namespaces are the exception: they carry `kustomize.toolkit.fluxcd.io/prune: disabled`, so dropping one from `namespaces.yaml` does not delete it and everything in it.

Run these from `infra/ansible` (`make deps` first, which installs `flux`, `kubectl`, `helm`, `sops` and `age` into `.venv/bin`):

| Target | What it does |
|---|---|
| `make flux-health` | Flux's controllers are healthy and every Kustomization is applied; lists what is deployed and at which commit |
| `make sync` | Fetch `develop` and apply it now instead of within the next minute |
| `make apps-diff` | Shows what this checkout's `infrastructure/`, `apps/` and `monitoring/` would change on the cluster. Deploys nothing |
| `make apps-health` | Checks the public route end to end: the Ingress has the hostname `ember.tail470a31.ts.net`, nginx answers `GET /healthz`, an unsigned POST to `/webhook/github` reaches intake and is refused (401 with `{"error": "invalid signature"}`), the same POST gets the same answer through Funnel's public address, and an unknown path returns 404. Also checks each enrich worker logged that it is listening. Deploys nothing |
| `make intake-secret` | Writes `apps/intake/webhooks.sops.yaml`, encrypted, from the GitHub App's webhook secret in the vault ([The webhook secret](#the-webhook-secret)). Commit the result |
| `make enrich-github-secret` | Writes `apps/enrich-github/github-app.sops.yaml`, encrypted, from the GitHub App's private key in the vault ([enrich-github](#enrich-github)). Commit the result |
| `make grafana-secret` | Writes the collector's and the Grafana Operator's Secrets, encrypted, from the Grafana Cloud values in the vault ([The Grafana Cloud tokens](#the-grafana-cloud-tokens)). Commit the result |
| `make grafana-health` | Checks Grafana Cloud end to end: the operator synced the stack and the Ember dashboard, and the stack has metrics from every node, intake, Postgres and Flux, pod logs, Kubernetes events and both synthetic checks. Deploys nothing |
| `make flux-preview BRANCH=<branch>` | Deploys a pushed branch instead of `develop`, to try a change before merging it. Tell the team, since it replaces what everyone else sees |
| `make flux-preview-end` | Goes back to `develop`, removing anything only the branch had |
| `make flux-up` | First install only, or after `make reset`: installs Flux from `clusters/homelab/flux-system`, gives it the age key, waits for the first sync, then health. Safe to re-run |
| `make flux-components` | Regenerates `clusters/homelab/flux-system/gotk-components.yaml` after `FLUX_VERSION` in the Makefile changes. Commit the result; Flux upgrades itself from it |

Day to day, `flux get all -A` (with `.venv/bin` on your `PATH`) shows the same thing as `flux-health`, and Headlamp (below) shows it in a browser. When a Kustomization is not Ready, `flux get kustomizations` shows why, and `flux logs --level=error` shows what the controllers rejected.

The `local-path-retain` StorageClass is not here: k3s deploys it on start from `ansible/manifests/`. Don't redefine it in this tree, or two things will own it.

### Secrets

Secrets live in Git only as SOPS-encrypted `*.sops.yaml` files. `.sops.yaml` at the repo root encrypts their `data` and `stringData` with an age key, and Flux decrypts them on the cluster with the private half, which `make flux-up` copies from the vault (`vault_sops_age_key`) into Secret `flux-system/sops-age`. CI fails on any `kind: Secret` that is not an encrypted `*.sops.yaml`.

To add one, write the plain Secret to a `*.sops.yaml` file under `infra/k8s`, encrypt it in place from the repo root, and list it in its directory's `kustomization.yaml`:

```bash
infra/ansible/.venv/bin/sops -e -i infra/k8s/<dir>/<name>.sops.yaml
```

Encrypting needs only the public key in `.sops.yaml`. Editing an existing one needs the private key:

```bash
cd infra/ansible
export SOPS_AGE_KEY="$(.venv/bin/ansible-vault view group_vars/all/vault.yml | sed -n 's/^  \(AGE-SECRET-KEY-.*\)/\1/p')"
.venv/bin/sops ../k8s/<dir>/<name>.sops.yaml
```

Keep a copy of the age key outside the vault too (a password manager). Without it nobody can decrypt or change the Secrets in Git.

## Plain-manifest apps

### nginx

The reverse proxy for the public hostname. A single `nginx:1.28-alpine` replica in namespace `ember` behind ClusterIP Service `nginx`. It is the only thing the public Ingress points at, so every request to `https://ember.tail470a31.ts.net` arrives here and nginx decides which Service gets it:

| Request | Where it goes |
|---|---|
| `POST /webhook/{source}` | intake on port 8080 (next section), which serves every source and answers 404 for one it doesn't have. Other methods are refused |
| `GET /healthz` | Answered by nginx itself with `ok`. It exists for the Deployment's probes and `make apps-health`, and does not depend on any other Service |
| Anything else | 404 |

The config is `apps/nginx/default.conf.template`. Kustomize ships it as a ConfigMap whose name ends in a hash of the file's contents, so an edit changes the name and Flux rolls the Deployment onto the new config by itself once it is merged. If nginx cannot load the edited file, the new pod fails to start, the old pod keeps serving, and the `apps` Kustomization reports not Ready (`flux get kustomizations`). The file is a template because the image fills in `${NGINX_LOCAL_RESOLVERS}` (the cluster's DNS server) when the container starts; leave that placeholder as it is.

A new source needs no route: it is an adapter in intake, under the same `/webhook/`. To add a route for something else, copy the `location /webhook/` block and change the path and the Service it proxies to. Keep the Service's address in a variable, as that block does (`set $intake intake-pods.ember.svc.cluster.local:8080;` then `proxy_pass http://$intake;`). With a variable, nginx looks the Service up when a request arrives instead of when it starts, so nginx starts and stays up even when that Service is absent. With the name written straight into `proxy_pass`, nginx would refuse to start until the Service exists.

The webhook route accepts request bodies up to 8 MiB (`client_max_body_size 8m`). That matches intake's limit (`MAX_BODY_BYTES` in `services/intake/intake.py`), so nginx and intake agree on which deliveries are too large. A route that does not set its own cap gets nginx's default of 1 MiB. nginx passes the body and GitHub's headers (`X-GitHub-*`, `X-Hub-Signature-256`) through unchanged, so a signature check behind it sees exactly what GitHub sent.

`apps/nginx/ingress.yaml` publishes nginx on the public internet through the Tailscale operator and Funnel (below). The hostname comes from `tls.hosts: [ember]` and is what the GitHub App registers as its callback and webhook URL, so it must not change. Add new backends as routes in nginx and leave the Ingress pointing at nginx. Nothing handles the App's callback URL yet, so that path returns 404 like any other; when something does, it is one more `location`.

### intake

The webhook listener for every source (EMBER-62): `services/intake`, whose README has the contract. Two replicas in namespace `ember` behind ClusterIP Service `intake` on port 8080, from `apps/intake/`. For each delivery it checks the signature, stores the body in Postgres `raw_events`, and answers 200 once that commits. It never calls a source API.

- **Image.** `deployment.yaml` pins a `develop-<run>-<sha>` tag of `ghcr.io/cbu-capstone-design-27/ember/intake` (`docs/container-images.md`). To deploy a newer intake, change the tag there and merge. The package is public, so there is no `imagePullSecret`.
- **Postgres.** `DATABASE_URL` comes from Secret `postgres-app` (Postgres below). A pod is ready only while `GET /health` reaches Postgres, so nginx never sends a delivery to a pod that cannot store it. The liveness probe asks `/metrics`, which doesn't touch Postgres, so a database outage makes the pods unready rather than restarting them.
- **Webhook secret.** `GITHUB_WEBHOOK_SECRET` comes from Secret `intake-webhooks` (next section). Intake refuses to start without it, so a missing Secret leaves the pods in `CreateContainerConfigError` and never stores an unsigned delivery.
- **Failover.** nginx reaches the pods through headless Service `intake-pods`, whose DNS name returns each ready pod's own address. When a pod doesn't accept a connection within 2 s, or drops a request it took, nginx sends the delivery to the other pod, inside GitHub's 10 s. This matters when a worker reboots: Kubernetes keeps listing its pod as ready for a minute or more, and before failover every delivery sent there was lost. A resend is safe because intake answers a delivery it already stored with 200 `duplicate`. Service `intake`, one virtual IP for both pods, stays for port-forwarding and in-cluster clients.
- **Stopping.** On SIGTERM a pod waits 5 s (`preStop`) while the Service and nginx stop sending it deliveries, then finishes the requests in flight, for up to 20 s. A rollout starts the new pod and waits for it to be ready before stopping an old one, and PodDisruptionBudget `intake` keeps one pod up through a drain or `make reboot`.
- **Memory.** About 60 MiB idle. The 1 GiB limit covers 8 deliveries near the 8 MiB cap at once, the most intake handles at a time.

Only `/webhook/` is public. `/health` and `/metrics` answer on the Service inside the cluster:

```bash
kubectl --context homelab -n ember port-forward svc/intake 8080 &
curl -s localhost:8080/metrics | grep intake_deliveries
```

To see deliveries:

```bash
kubectl --context homelab -n ember logs -l app=intake --prefix
```

Each stored or duplicate delivery logs one line with the source, the result, the delivery id, the event and the account, never the payload. Intake doesn't log refused deliveries: they show up in `/metrics` (`result="unauthorized"` for a bad signature) and in the App's Recent Deliveries on GitHub. nginx logs every webhook request, refused or not, with its status and sender:

```bash
kubectl --context homelab -n ember logs deploy/nginx | grep ' event='
```

`target=integration:5075660` is the `ember-ingest` App. `target=repository:<id>` or `organization:<id>` is a webhook set directly on a repo or org (`gh api repositories/<id> --jq .full_name` names the repo); if it answers 401, that webhook has a different secret or none.

#### The webhook secret

GitHub signs each delivery with the App's webhook secret, and intake checks it. The value lives in three places that must match: the GitHub App's settings, the vault, and `apps/intake/webhooks.sops.yaml`. To set or rotate it:

1. Generate one: `openssl rand -hex 32`.
2. Keep it in the vault as `vault_github_webhook_secret` (`.venv/bin/ansible-vault edit group_vars/all/vault.yml` from `infra/ansible`). The vault is where it lives; the other two copies are made from it.
3. `make intake-secret` (from `infra/ansible`) writes `apps/intake/webhooks.sops.yaml`, Secret `intake-webhooks`, encrypted with the public key in `.sops.yaml`. It never prints the value, and it leaves the file alone when it already holds the vault's value, so running it again makes no diff.
4. Commit and merge the file. A changed Secret reaches running pods only after `kubectl --context homelab -n ember rollout restart deploy/intake`.
5. In the GitHub App's settings (`ember-ingest`), under Webhook: set the secret, the URL `https://ember.tail470a31.ts.net/webhook/github`, and Active.

Between steps 4 and 5 GitHub's deliveries are refused with 401. GitHub doesn't retry them, but each one can be redelivered from the App's Advanced tab, Recent Deliveries, for 3 days.

### enrich-github

`enrich:github` (EMBER-65, `services/github-ingestion`): turns the GitHub rows intake stores in `raw_events` into `source_records`. One replica in namespace `ember` from `apps/enrich-github/`, with no Service, because it serves nothing. It claims rows with `FOR UPDATE SKIP LOCKED`, so a rollout's overlap never enriches a row twice.

- **Image.** `deployment.yaml` pins a `develop-<run>-<sha>` tag of `ghcr.io/cbu-capstone-design-27/ember/github-ingestion`, as intake does.
- **Credentials.** `DATABASE_URL` comes from Secret `postgres-app`. `GITHUB_APP_ID` is set in the Deployment (it is not a secret). The App's private key is Secret `enrich-github-app`, mounted as a file at `/etc/github-app/private-key.pem`. Intake never sees the key, and this worker never sees the webhook secret (`docs/ingestion/architecture.md`, Credentials).
- **Tenants.** Intake stores a delivery with `tenant` null until its installation id is in `installations`, and the worker leaves those rows alone. Until `ember-admin map` exists, map an installation with SQL: `insert into installations (source, account_id, tenant) values ('github', '<installation id>', '<tenant>')`, then `update raw_events set tenant = '<tenant>' where source = 'github' and account_id = '<installation id>' and tenant is null`.
- **Stopping.** On SIGTERM it stops claiming and finishes the rows in flight, for up to 60 s (`terminationGracePeriodSeconds`). A row cut off by the kill is rolled back and claimed again by the next pod.

```bash
kubectl --context homelab -n ember logs deploy/enrich-github
```

It logs `enrich:github listening on raw_events` once connected, and one line per row with how many `source_records` it wrote, never the payload.

To set or rotate the key: generate a private key in the App's settings (`ember-ingest`, Private keys), put the whole `.pem` in the vault as `vault_github_app_private_key` (a YAML block scalar, `|`), run `make enrich-github-secret`, commit the file, and after it merges `kubectl --context homelab -n ember rollout restart deploy/enrich-github`. Delete the old key on GitHub once the new pod is listening.

### Restarts

nginx and intake come back by themselves after a pod or node restart. Their Deployments, Services, ConfigMaps and the Ingress are stored in the cluster, k3s starts on boot on every node, the Tailscale proxy keeps its identity and certificate in a Secret, and a pod restarting on the same node uses the image already cached there.

The limits:

- A pod that is moved to the other worker needs its registry to be reachable (Docker Hub for nginx, GHCR for intake), because its image is cached only on the worker that ran it before.
- Everything depends on pi-cp, which runs the API server, the datastore and the only CoreDNS replica.
- nginx and the Tailscale proxy are single pods, so the public URL is down while either one restarts. A second nginx replica would not help, because the Tailscale proxy is not moved to another node while its own node is down.

To check, delete the pods (`kubectl --context homelab -n ember delete pod -l 'app in (nginx, intake)'`) or reboot every node with `make reboot`, then run `make apps-health` and `make ts-operator-health`.

### Troubleshooting the public route

| Symptom | Check |
|---|---|
| `502 Bad Gateway` on `/webhook/github` | nginx is up but cannot reach intake: no intake pod is ready, or Service `intake-pods` is missing. `kubectl --context homelab -n ember get deploy,svc,pods -l app=intake`. Pods that never get ready usually mean Postgres is down (`make postgres-health`); `CreateContainerConfigError` means Secret `intake-webhooks` or `postgres-app` is missing |
| GitHub's Recent Deliveries show 401 | The App's webhook secret and Secret `intake-webhooks` differ, or the pods still run with the old one. Set them to the same value (The webhook secret, above) and restart intake |
| `make apps-health` fails on the hostname check | The Tailscale device was recreated as `ember-1`, which is not the hostname GitHub posts to. Remove the stale `ember` device in the Tailscale admin console, then `kubectl --context homelab -n ember delete ingress ember` and `make sync`; Flux recreates the Ingress and the proxy comes back as `ember` |
| After a config edit, the new nginx pod crash-loops while the old one keeps serving | nginx could not load the edited config. `kubectl --context homelab -n ember get pods -l app=nginx` names the pod that is restarting, and `kubectl --context homelab -n ember logs <that pod>` shows the line nginx rejected. Fix `apps/nginx/default.conf.template` and merge the fix |

## Tailscale operator

Serves Ingresses with `ingressClassName: tailscale`. Each one gets a proxy that joins the tailnet as its own device named after `tls.hosts`, with a Let's Encrypt certificate for `https://<name>.tail470a31.ts.net`. The `tailscale.com/funnel: "true"` annotation also opens it to the internet. Flux installs it from `infrastructure/tailscale-operator/`: chart version pinned in `helmrelease.yaml`, values in `values.yaml`.

One-time setup in the Tailscale admin console:

1. **DNS**: enable MagicDNS and HTTPS certificates.
2. **Access controls**: add
   ```json
   "tagOwners": {
     "tag:k8s-operator": ["autogroup:admin"],
     "tag:k8s": ["tag:k8s-operator"]
   },
   "nodeAttrs": [{ "target": ["tag:k8s"], "attr": ["funnel"] }]
   ```
   Funnel needs the `nodeAttrs` entry even if `autogroup:member` already has `funnel`, because tagged devices aren't members.
3. **Settings > Trust credentials**: Credential > OAuth, Read and Write on Devices > Core, Keys > Auth Keys and General > Services, tag `tag:k8s-operator`. Put the ID and secret in `infrastructure/tailscale-operator/operator-oauth.sops.yaml` as `client_id` and `client_secret` (see Secrets above).

`make ts-operator-health` (from `infra/ansible`) checks that the operator is ready and logged in, that each Tailscale Ingress has a hostname and answers over HTTPS, and that each Funnel Ingress also has a public DNS record.

A changed OAuth secret only takes effect after `kubectl --context homelab -n tailscale rollout restart deploy/operator`.

Requests from a tailnet device resolve the name through MagicDNS and go straight to the proxy, so they work whether or not Funnel does. Test Funnel from a device off the tailnet, or rely on the public DNS check in `ts-operator-health`.

| Symptom | Check |
|---|---|
| The URL loads on tailnet devices, everything else gets a DNS error | The proxy has no `funnel` attribute, so the name was never published. Add the `nodeAttrs` entry above; the record appears within a minute or two. Resolvers cache the earlier "not found" for up to 5 minutes |
| Still no public record after the policy change | `kubectl --context homelab -n tailscale rollout restart statefulset -l tailscale.com/parent-resource=ember` |
| `ts-operator-health` fails on "Check the operator logged in" | The OAuth credential lacks a scope or the `tag:k8s-operator` tag |
| First request hangs for up to a minute | The proxy is fetching its Let's Encrypt certificate; later requests are fast |

## Helm charts

Each chart is a Flux `HelmRelease` next to a `HelmRepository` and the chart's `values.yaml`. The chart version is pinned in `helmrelease.yaml`, so a reinstall doesn't pick up a newer chart; to upgrade, change it there. Kustomize ships `values.yaml` as a ConfigMap with a hash in its name, so an edit to it changes the HelmRelease and Flux upgrades the release. `flux get helmreleases -A` shows each release's state, and `helm --kube-context homelab history <release> -n <namespace>` its revisions.

### Neo4j

Community edition, release `neo4j` in namespace `ember`, pinned to hv-worker-1 with a 10Gi `local-path-retain` volume. `apps/neo4j/service-lb.yaml` publishes it on every node's IP through k3s ServiceLB, tailnet sources only. `make neo4j-health` checks that the pod is ready on hv-worker-1, answers `RETURN 1` inside the pod, that each node's `<node>.tail470a31.ts.net` name resolves to its IP, and that an authenticated query works on port 7474 of every node.

Connect with user `neo4j`:

- From the tailnet: `bolt://hv-worker-1.tail470a31.ts.net:7687`, Browser at `http://hv-worker-1.tail470a31.ts.net:7474`. The full name resolves on any tailnet device, including ones that don't use the tailnet's search domain. `pi-cp.tail470a31.ts.net` and `hv-worker-2.tail470a31.ts.net` work too, since ServiceLB forwards from every node. Use `bolt://`, not `neo4j://`: the routing scheme reconnects to the server's advertised address, which isn't set to the node's name.
- In-cluster: `bolt://neo4j.ember.svc.cluster.local:7687`.

The data volume outlives the release. To wipe it, `flux suspend helmrelease neo4j -n ember`, uninstall with `helm --kube-context homelab -n ember uninstall neo4j`, delete PVC `data-neo4j-0`, its PV and the PV's directory under `/var/lib/rancher/k3s/storage` on hv-worker-1, then `flux resume helmrelease neo4j -n ember` to install it fresh.

### Postgres

The ingestion store (`docs/ingestion/architecture.md`, Storage; ADR 0005). One instance in namespace `ember`, run by the CloudNativePG operator from `apps/postgres/cluster.yaml`: Cluster `postgres`, database `ember` owned by role `ember`, pinned to hv-worker-2 with a 10Gi `local-path-retain` volume. The operator itself is release `cnpg` in namespace `cnpg-system`, from `infrastructure/cnpg/`, so its CRDs exist before `apps/` is applied. `make postgres-health` checks that the instance is ready on hv-worker-2, that the migrations ran and the ingestion tables exist, and that Postgres answers on every node's IP.

The operator generates role `ember`'s password and keeps it in Secret `postgres-app`; it is not in Git. The Secret's `uri` key is the in-cluster connection string, `postgresql://ember:<password>@postgres-rw.ember:5432/ember`, and is how a service gets `DATABASE_URL`:

```yaml
env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: postgres-app
        key: uri
```

Service `postgres-rw` always points at the primary. Every component connects as `ember` for now; ADR 0004's role per component comes later.

`ember` is not a superuser, and superuser access is off (the operator's default), so no Secret holds a superuser password. An extension only a superuser can create, such as `vector` (pgvector, EMBER-77), goes in `apps/postgres/database.yaml`, a CloudNativePG `Database` for `ember` with `extensions: [{name: ..., ensure: present}]`. The operator creates it there, and a migration's `create extension if not exists` then finds it and needs no privilege. Merge that change before the migration that uses it. The image (`imageName` in `cluster.yaml`) must ship the extension: `select * from pg_available_extensions` in the pod lists what it has.

From the tailnet, `make postgres-url` prints `postgresql://ember:<password>@hv-worker-2.tail470a31.ts.net:5432/ember`. As with Neo4j, `apps/postgres/service-lb.yaml` publishes port 5432 on every node's IP through ServiceLB, tailnet sources only, so the other nodes' names work too. Reading the Secret needs the kubeconfig: Headlamp's viewer cannot see Secrets.

#### Migrations

The schema is SQL files in `apps/postgres/migrations/`, applied by Job `postgres-migrate` with [dbmate](https://github.com/amacneil/dbmate). dbmate records each migration it applies in table `schema_migrations` and only runs ones that are not there, each in its own transaction. To change the schema:

1. Change the SQL in `docs/ingestion/architecture.md` (Storage). It is the source of truth.
2. Add `migrations/<UTC timestamp>_<what>.sql` with a `-- migrate:up` and a `-- migrate:down` section (`dbmate -d migrations new <what>` from `apps/postgres` writes the skeleton). Never edit a migration that has been merged: the cluster has already applied it and will not run it again.
3. List the file under `configMapGenerator` in `apps/postgres/kustomization.yaml`.
4. CI runs `apps/postgres/test_migrations.py`. It applies the migrations, rolls them all back, applies them again, and fails if the result differs from the doc's schema or if a migration is missing from the kustomization.

Once merged, the new file changes the hashed name of the ConfigMap the Job mounts. A Job's pod template cannot be edited, so the Job carries `kustomize.toolkit.fluxcd.io/force: enabled` and Flux deletes and recreates it. The new Job applies what is new. If a migration fails, the Job is marked failed and the `apps` Kustomization goes not Ready. `kubectl --context homelab -n ember logs job/postgres-migrate` shows the error. The failed migration's transaction rolled back and dbmate did not record it, so fixing that file and merging the fix is enough.

To run the test locally you need `psql`, `dbmate`, and a Postgres it may create databases on:

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 pgvector/pgvector:0.8.7-pg18-trixie
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
  python3 infra/k8s/apps/postgres/test_migrations.py
```

### Headlamp

A web UI for the cluster, with the Flux plugin for what Flux has deployed: `https://headlamp.tail470a31.ts.net`, on the tailnet only (its Ingress has no Funnel annotation). Release `headlamp` in namespace `headlamp`, from `apps/headlamp/`.

Headlamp asks for a token. Get one valid for 24 hours with `make headlamp-token` (from `infra/ansible`). It signs you in as ServiceAccount `headlamp-viewer`, which has the built-in `view` role plus Flux's objects, so it can look at everything except Secrets and change nothing. Use `make sync` or `flux` for actions.

The Flux plugin comes from the image `ghcr.io/headlamp-k8s/headlamp-plugin-flux`, pinned in `values.yaml`; an init container copies it into the pod.

### Monitoring

Grafana's `k8s-monitoring` chart (EMBER-91): release `k8s-monitoring` in namespace `monitoring`, from `monitoring/k8s-monitoring/`. Flux applies it as its own Kustomization, `monitoring`. It sends everything to the Grafana Cloud stack `https://zestylagoon2114.grafana.net`, owned by Jacob Pugh (Infrastructure Lead). The stack is on the free tier: 10k active metric series, 50 GB of logs a month, and 14 days' retention. Nothing is stored on the cluster. The Grafana Operator keeps the dashboard there from Git ([Dashboards](#dashboards)).

**Where to look.** All of these are at `https://zestylagoon2114.grafana.net`. `make grafana-health` checks that each one has data.

| What | Where |
|---|---|
| Nodes, pods, Postgres, webhooks, the enrich backlog, Flux | Dashboard **Ember** (folder Ember, `/d/ember`) |
| One node, pod or workload in depth | The Kubernetes Monitoring app (Kubernetes in Grafana's menu). It reads the same metrics |
| A metric or a log line | Explore. Metrics: data source `grafanacloud-zestylagoon2114-prom`, for example `up{cluster="homelab"}` or `intake_deliveries_total`. Logs: `grafanacloud-zestylagoon2114-logs`, for example `{namespace="ember", container="nginx"} \|= " event="` |
| Kubernetes events, such as a node's Rebooted or NodeNotReady | Explore, logs: `{job="integrations/kubernetes/eventhandler"}` |
| The public URL from outside | Synthetics in Grafana's menu ([Synthetic checks](#synthetic-checks)) |

| Workload | Where it runs | What it collects |
|---|---|---|
| Alloy `alloy-metrics` | One pod, preferring hv-worker-2 | Scrapes the kubelet, cAdvisor, kube-state-metrics, Node Exporter and annotated pods into Prometheus (Mimir), and Kubernetes events into Loki |
| Alloy `alloy-logs` | Every node, pi-cp included | Every pod's log, read from `/var/log/pods`, into Loki |
| kube-state-metrics | One pod, preferring hv-worker-2 | Node, pod and workload state, and Flux's Kustomizations, HelmReleases and sources as `gotk_resource_info` (Flux's documented setup) |
| Node Exporter | Every node, pi-cp included | CPU, memory, disk and network, and `node_boot_time_seconds`, which changes on every reboot |

The Alloy operator, also in `monitoring`, runs the two Alloy collectors from the chart's `Alloy` objects. `alloy-metrics` is a Deployment rather than the chart's usual StatefulSet: when its node goes down, the pod is recreated on another node once Kubernetes evicts it, about 5 minutes later. A StatefulSet's pod would wait for the node to come back. It and kube-state-metrics prefer hv-worker-2 because hv-worker-1 reboots often (EMBER-90).

The collector scrapes a pod that carries these annotations:

```yaml
annotations:
  k8s.grafana.com/scrape: "true"
  k8s.grafana.com/metrics.portName: http   # the container port that serves /metrics
```

intake has them (`intake_deliveries_total{source,result}`), and so does Postgres (below). New workers should expose `/metrics` and carry the same annotations. Only pods are discovered, not Services. Every series counts against the free tier's 10k, so keep label values bounded. intake, for example, counts a made-up source as `unknown` rather than by name.

**Postgres.** CloudNativePG's exporter (`cnpg_*`, port 9187) runs the operator's default queries plus two of ours, from `apps/postgres/monitoring.yaml`:

| Metric | What it counts |
|---|---|
| `cnpg_raw_events_backlog_rows{source}` | Rows waiting for that source's enrich worker: `enriched_at` null, tenant set |
| `cnpg_raw_events_backlog_oldest_age_seconds{source}` | How long the oldest of those has waited since it arrived |
| `cnpg_raw_events_unmapped_rows{source}` | Rows from accounts with no `installations` row (tenant null). Enrich never claims these |
| `cnpg_raw_events_unmapped_accounts{source}` | Distinct accounts among those rows |

A source with no such rows exports no series, so it shows as missing rather than 0. The queries run as role `cnpg_metrics_exporter`. Migration `20261015120000_raw_events_metrics_grant.sql` lets that role read only the columns these queries use, never the envelope. A query that needs another column needs a new migration that grants it.

**Volume fill.** The kubelet reports nothing for local-path volumes. They are plain directories (`hostPath`), and their 10Gi size isn't enforced, so Postgres and Neo4j can grow until their node's disk is full. To see how full they are, watch that disk (`node_filesystem_avail_bytes{mountpoint="/"}`: Postgres on hv-worker-2, Neo4j on hv-worker-1), and Postgres's own size, `cnpg_pg_database_size_bytes`.

#### Dashboards

The Grafana Operator (release `grafana-operator` in namespace `monitoring`, from `infrastructure/grafana-operator/`) keeps folder Ember and dashboard Ember on the stack. It signs in to the stack as Grafana object `grafana-cloud` (`monitoring/grafana/grafana.yaml`). The dashboard is `monitoring/grafana/dashboards/ember.json`, applied by `GrafanaDashboard` `ember`. The operator is in `infrastructure/` so that its CRDs exist before `monitoring/` uses them.

Git is the source of truth: the operator overwrites an edit made in Grafana at its next sync, within 10 minutes. To change the dashboard:

1. Edit it in Grafana, then export it as JSON (Export in the dashboard's toolbar), with the option to share it with another instance left off.
2. Replace `dashboards/ember.json` with the file, keeping `"uid": "ember"`, and merge.

Each panel's data source is the dashboard's Metrics or Logs variable, which finds the stack's `…-prom` and `…-logs` data sources by name. To add a dashboard, add its JSON under `dashboards/`, list it in `monitoring/grafana/kustomization.yaml` and add a `GrafanaDashboard` like `ember`'s to `dashboards.yaml`.

`kubectl --context homelab -n monitoring get grafana,grafanadashboard,grafanafolder` shows whether the operator synced each one, and `describe` says why one didn't.

#### Synthetic checks

Grafana Cloud Synthetic Monitoring tests the public URL from outside, which nothing on the cluster can do: Funnel, DNS and the Tailscale proxy are all in the path. The two checks were set up by hand under Synthetics in Grafana's menu, as Scripted (k6) checks. The operator doesn't manage Synthetic Monitoring, so this section is the record. To rebuild one, create a Scripted check with its job name, its URL as the instance, and its script. Run it from one probe location (Calgary) every 10 minutes, with alerting off.

| Job name | What it proves |
|---|---|
| `ember-healthz` | Funnel, DNS, the Tailscale proxy and nginx answer. nginx returns `ok` itself, without asking intake |
| `ember-webhook-unsigned` | The webhook route reaches intake. Only intake turns away an unsigned delivery with 401 `invalid signature` |

`ember-healthz`:

```js
import { expect } from 'https://jslib.k6.io/k6-testing/0.5.0/index.js';
import http from 'k6/http';

// Funnel, DNS, the Tailscale proxy and nginx answer (nginx returns "ok" itself, without asking intake)
export default function main() {
  const resp = http.get('https://ember.tail470a31.ts.net/healthz', { responseType: 'text' });
  expect(resp.status).toBe(200);
  expect(resp.body.trim()).toBe('ok');
}
```

`ember-webhook-unsigned`:

```js
import { expect } from 'https://jslib.k6.io/k6-testing/0.5.0/index.js';
import http from 'k6/http';

// An unsigned delivery is turned away by intake itself, which proves the route reaches intake.
// Each run adds 1 to intake_deliveries_total{result="unauthorized"}.
export default function main() {
  const resp = http.post('https://ember.tail470a31.ts.net/webhook/github', '{}', {
    headers: {
      'Content-Type': 'application/json',
      'X-GitHub-Event': 'ping',
      'X-GitHub-Delivery': 'synthetic-check',
    },
    responseType: 'text',
    // 401 is the answer this check wants, so k6 shouldn't count it as a failed request
    responseCallback: http.expectedStatuses(401),
  });
  expect(resp.status).toBe(401);
  expect(resp.json('error')).toBe('invalid signature');
}
```

The scripts ask for `responseType: 'text'` because the k6 version Synthetic Monitoring runs returns the body as binary otherwise. Test a changed script with Test on the check's page before saving. Together the two run about 8,600 times a month of the free tier's allowance. Each run of the second counts once in `intake_deliveries_total{result="unauthorized"}`, so the unauthorized count never drops to 0. Their results are metrics on the stack, such as `probe_success{job="ember-healthz"}`, which `make grafana-health` checks.

#### Free tier usage

Free-tier usage is on grafana.com: the organization's usage page shows active series and the month's log gigabytes against the limits. In Grafana, data source `grafanacloud-usage` holds the same numbers, for example `grafanacloud_instance_active_series`. EMBER-91 measured about 3,900 active series out of 10k. Before adding a scrape, check what it adds. The collector's own count of the series it sends is `prometheus_remote_write_wal_storage_active_series`:

```bash
kubectl --context homelab -n monitoring port-forward deploy/k8s-monitoring-alloy-metrics 12345 &
curl -s localhost:12345/metrics | grep -E '^prometheus_remote_write_wal_storage_active_series|^prometheus_remote_storage_samples_failed_total'
```

To cut series, drop metrics with `metricsTuning.excludeMetrics` under the feature in `monitoring/k8s-monitoring/values.yaml`. In intake and future workers, keep label values bounded.

#### The Grafana Cloud tokens

There are two tokens:

- **Collector:** pushes metrics and logs with a Grafana Cloud access policy token that can only write them (`metrics:write`, `logs:write`). The token, the push URLs and the user IDs live in the vault and in Secret `grafana-cloud` (`monitoring/k8s-monitoring/grafana-cloud.sops.yaml`).
- **Grafana Operator:** manages the dashboards with a token for a Grafana service account, kept in the vault and in Secret `grafana-operator` (`monitoring/grafana/grafana-operator.sops.yaml`).

| Vault variable | Secret key | Where it comes from, in the Grafana Cloud portal (grafana.com) |
|---|---|---|
| `vault_grafana_cloud_prometheus_url` | `prometheus-url` | The stack's Prometheus details: the remote write endpoint, ending `/api/prom/push` |
| `vault_grafana_cloud_prometheus_user` | `prometheus-username` | Same page: the username (instance ID) |
| `vault_grafana_cloud_loki_url` | `loki-url` | The stack's Loki details: the URL (`https://logs-prod-<n>.grafana.net`). `make grafana-secret` adds `/loki/api/v1/push` |
| `vault_grafana_cloud_loki_user` | `loki-username` | Same page: the user |
| `vault_grafana_cloud_access_token` | `access-token` | Access Policies: a policy for this stack with `metrics:write` and `logs:write` only, then Add token |
| `vault_grafana_cloud_sa_token` | `token` (Secret `grafana-operator`) | In Grafana (`zestylagoon2114.grafana.net`): Administration → Users and access → Service accounts, account `k3-dashboards`, role Editor (with no role it can only read folders, and every sync fails 403), Add service account token |

To set the values or rotate a token:

1. Create the new token: under Access Policies for the collector, or on service account `k3-dashboards` for the operator.
2. Put the values in the vault (`.venv/bin/ansible-vault edit group_vars/all/vault.yml` from `infra/ansible`).
3. `make grafana-secret` (from `infra/ansible`) writes both encrypted Secrets.
   - It never prints a value.
   - It leaves a file alone when it already holds the vault's values, so running it again makes no diff.
   - It refuses a Prometheus URL that is not the full remote write endpoint and a Loki URL with any other path.
   - It names any vault variable that is missing.
4. Commit and merge the files. The collector and the operator read their Secret again within a minute or two, with no restart.
5. Once `make grafana-health` passes, delete the old token.

If the collector's token is wrong or expired, nothing arrives and the collectors log the refusal. `alloy-metrics` sends the metrics and events, and the `alloy-logs` pods send the pod logs:

```bash
kubectl --context homelab -n monitoring logs deploy/k8s-monitoring-alloy-metrics -c alloy | grep -E 'level=(error|warn)'
kubectl --context homelab -n monitoring logs -l app.kubernetes.io/name=alloy-logs -c alloy --prefix | grep -E 'level=(error|warn)'
```

If the operator's token is wrong, `kubectl --context homelab -n monitoring describe grafana grafana-cloud` shows the 401, and the dashboard stops updating.
