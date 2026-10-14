# k8s

Manifests for the `homelab` k3s cluster. Node and k3s setup live in `../ansible` (see `../docs/k3-setup.md`).

| Directory | What goes in it |
|---|---|
| `clusters/homelab/` | Flux itself (`flux-system/`) and the two Flux `Kustomization`s that point it at the directories below |
| `infrastructure/` | Cluster-wide pieces: namespaces, CRDs, operators, ingress, cert-manager |
| `apps/` | Ember workloads, one subdirectory per app, each listed in `apps/kustomization.yaml` |

## How deploys work

The full guide to Flux here (why, layout, secrets, recovery, troubleshooting) is `../docs/flux.md`.

Flux runs on the cluster and follows the `develop` branch (ADR 0003). About once a minute it fetches `infra/k8s`, applies `infrastructure/`, waits until everything there is healthy, then applies `apps/`. **Merging to `develop` is the deploy.** Nobody runs `kubectl apply` or `helm` against the cluster.

Flux also deletes what is removed from Git (pruning), including the old hashed ConfigMaps a config edit leaves behind. Namespaces are the exception: they carry `kustomize.toolkit.fluxcd.io/prune: disabled`, so dropping one from `namespaces.yaml` does not delete it and everything in it.

Run these from `infra/ansible` (`make deps` first, which installs `flux`, `kubectl`, `helm`, `sops` and `age` into `.venv/bin`):

| Target | What it does |
|---|---|
| `make flux-health` | Flux's controllers are healthy and every Kustomization is applied; lists what is deployed and at which commit |
| `make sync` | Fetch `develop` and apply it now instead of within the next minute |
| `make apps-diff` | Shows what this checkout's `infrastructure/` and `apps/` would change on the cluster. Deploys nothing |
| `make apps-health` | Checks the public route end to end: the Ingress has the hostname `ember.tail470a31.ts.net`, nginx answers `GET /healthz`, a POST to `/webhook/github` reaches the placeholder listener (the response's `listener` field says `placeholder`), the same POST works through Funnel's public address, and an unknown path returns 404. Deploys nothing |
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
| `POST /webhook/github` | Service `github-webhook` on port 8080 (next section). Other methods on this path are refused |
| `GET /healthz` | Answered by nginx itself with `ok`. It exists for the Deployment's probes and `make apps-health`, and does not depend on any other Service |
| Anything else | 404 |

The config is `apps/nginx/default.conf.template`. Kustomize ships it as a ConfigMap whose name ends in a hash of the file's contents, so an edit changes the name and Flux rolls the Deployment onto the new config by itself once it is merged. If nginx cannot load the edited file, the new pod fails to start, the old pod keeps serving, and the `apps` Kustomization reports not Ready (`flux get kustomizations`). The file is a template because the image fills in `${NGINX_LOCAL_RESOLVERS}` (the cluster's DNS server) when the container starts; leave that placeholder as it is.

To add a route, copy the `location = /webhook/github` block and change the path and the Service it proxies to. Keep the Service's address in a variable, as that block does (`set $github_webhook github-webhook.ember.svc.cluster.local:8080;` then `proxy_pass http://$github_webhook;`). With a variable, nginx looks the Service up when a request arrives instead of when it starts, so nginx starts and stays up even when that Service is absent. With the name written straight into `proxy_pass`, nginx would refuse to start until the Service exists.

The webhook route accepts request bodies up to 8 MiB (`client_max_body_size 8m`). That matches intake's limit (`MAX_BODY_BYTES` in `services/intake/intake.py`), so nginx and intake agree on which deliveries are too large. A route that does not set its own cap gets nginx's default of 1 MiB. nginx passes the body and GitHub's headers (`X-GitHub-*`, `X-Hub-Signature-256`) through unchanged, so a signature check behind it sees exactly what GitHub sent.

`apps/nginx/ingress.yaml` publishes nginx on the public internet through the Tailscale operator and Funnel (below). The hostname comes from `tls.hosts: [ember]` and is what the GitHub App registers as its callback and webhook URL, so it must not change. Add new backends as routes in nginx and leave the Ingress pointing at nginx. Nothing handles the App's callback URL yet, so that path returns 404 like any other; when something does, it is one more `location`.

### github-webhook

A placeholder listener, not the real GitHub ingestion worker. It is there to prove that a delivery from GitHub reaches a container on the cluster before the worker is deployed (EMBER-56). One `python:3.12-slim` replica in namespace `ember` behind ClusterIP Service `github-webhook` on port 8080.

On `POST /webhook/github` it reads the body, discards it, and returns 200 with `{"status": "ok", "listener": "placeholder"}`. It logs one line per delivery with the event name, the delivery id, the body size and whether a signature header was present. It stores nothing and never logs the payload.

It does not verify signatures, so anyone on the internet can POST to it. That is acceptable only because it stores nothing; the real worker has to check the signature.

The code is `apps/github-webhook/listener.py`. It predates the GHCR images (`docs/container-images.md`), so the file is shipped as a ConfigMap and mounted into a stock `python:3.12-slim` image instead of being built into one. As with nginx, the ConfigMap's name carries a content hash, so an edit to `listener.py` rolls the Deployment once Flux applies it.

To see deliveries:

```bash
kubectl --context homelab -n ember logs deploy/github-webhook
```

Lines with the delivery id `apps-health` are the test deliveries that `make apps-health` sends.

To watch a real delivery arrive, set the GitHub App's webhook URL to `https://ember.tail470a31.ts.net/webhook/github`. GitHub then sends a `ping`, which shows up in those logs. Do this for that one `ping` only. The placeholder answers 200 and there is no secret, so GitHub records every delivery as successful while the placeholder throws it away. Once you have seen the `ping`, clear the webhook URL (or subscribe the App to no events) until the real worker is in.

Service `github-webhook` on port 8080 is the stable name nginx proxies to. EMBER-62 replaces it with `services/intake`, which has the webhook secret and the signature check. Three things to know when doing that:

- Flux prunes, so taking the placeholder Deployment out of `apps/github-webhook/` deletes it. Keep the pods' label `app: github-webhook` if you replace the Deployment's pod template in place instead (a Deployment's selector cannot be changed), so Service `github-webhook` keeps finding them.
- The webhook checks in `make apps-health` look for `"listener": "placeholder"` in the response. Update them in `ansible/playbooks/apps.yml` when the real worker goes in.
- Intake (EMBER-62) replaces it, from the public `intake` image on GHCR, so no `imagePullSecret` is needed.

### Restarts

nginx and the listener come back by themselves after a pod or node restart. Their Deployments, Services, ConfigMaps and the Ingress are stored in the cluster, k3s starts on boot on every node, the Tailscale proxy keeps its identity and certificate in a Secret, and a pod restarting on the same node uses the image already cached there.

The limits:

- A pod that is moved to the other worker needs Docker Hub to be reachable, because its image is cached only on the worker that ran it before.
- Everything depends on pi-cp, which runs the API server, the datastore and the only CoreDNS replica.
- nginx and the Tailscale proxy are single pods, so the public URL is down while either one restarts. A second nginx replica would not help, because the Tailscale proxy is not moved to another node while its own node is down.

To check, delete the pods (`kubectl --context homelab -n ember delete pod -l 'app in (nginx, github-webhook)'`) or reboot every node with `make reboot`, then run `make apps-health` and `make ts-operator-health`.

### Troubleshooting the public route

| Symptom | Check |
|---|---|
| `502 Bad Gateway` on `/webhook/github` | nginx is up but cannot reach the listener: the listener is not running, or Service `github-webhook` is missing. `kubectl --context homelab -n ember get deploy,svc github-webhook` |
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
docker run -d --name pg -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 postgres:18.6
PGTEST_URL='postgresql://postgres:pw@127.0.0.1:55432/postgres?sslmode=disable' \
  python3 infra/k8s/apps/postgres/test_migrations.py
```

### Headlamp

A web UI for the cluster, with the Flux plugin for what Flux has deployed: `https://headlamp.tail470a31.ts.net`, on the tailnet only (its Ingress has no Funnel annotation). Release `headlamp` in namespace `headlamp`, from `apps/headlamp/`.

Headlamp asks for a token. Get one valid for 24 hours with `make headlamp-token` (from `infra/ansible`). It signs you in as ServiceAccount `headlamp-viewer`, which has the built-in `view` role plus Flux's objects, so it can look at everything except Secrets and change nothing. Use `make sync` or `flux` for actions.

The Flux plugin comes from the image `ghcr.io/headlamp-k8s/headlamp-plugin-flux`, pinned in `values.yaml`; an init container copies it into the pod.
