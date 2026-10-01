# k8s

Manifests for the `homelab` k3s cluster. Node and k3s setup live in `../ansible` (see `../docs/k3-setup.md`).

| Directory | What goes in it |
|---|---|
| `infrastructure/` | Cluster-wide pieces: namespaces, CRDs, operators, ingress, cert-manager |
| `apps/` | Ember workloads, one subdirectory per app. Plain manifests are listed in `apps/kustomization.yaml`; Helm apps hold a `values.yaml` instead (see below) |

Each directory is a kustomize root. Until Flux is in place, deploy them from `infra/ansible` with the targets below. All but `apps-diff` run the Ansible playbook `ansible/playbooks/apps.yml`.

| Target | What it does |
|---|---|
| `make apps-diff` | Shows what `apps-up` would change on the cluster. Deploys nothing |
| `make apps-up` | Applies `infrastructure/`, then `apps/`, waits for every workload in `apps/` to roll out, then runs the `apps-health` checks. Safe to re-run |
| `make apps-health` | Checks the public route end to end: the Ingress has the hostname `ember.tail470a31.ts.net`, nginx answers `GET /healthz`, a POST to `/webhook/github` reaches the placeholder listener (the response's `listener` field says `placeholder`), the same POST works through Funnel's public address, and an unknown path returns 404. Deploys nothing |
| `make apps-down` | Deletes what is in `apps/` and leaves `infrastructure/` in place. Read the two notes below before running it |

The health checks need the Ingress to have its hostname, which the Tailscale operator sets, so run `make ts-operator-up` (below) before the first `make apps-up`.

`make apps-down` deletes the Ingress, and with it the Secret where the Tailscale proxy keeps its certificate. The next `apps-up` then has to get a new certificate for the hostname GitHub posts to. Avoid down/up cycles for that reason: to change something, edit the manifests and run `make apps-up` again.

`make apps-down` also deletes every ConfigMap in namespace `ember` labelled `ember.io/generated=true`. Each config edit creates a ConfigMap with a new name and leaves the old one behind, so the kustomizations listed in `apps/kustomization.yaml` put that label on the ConfigMaps they generate, and `apps-down` uses it to remove them all. The label is reserved for those kustomizations: do not put it on anything `apps-down` should leave alone, such as something under `apps/neo4j/`.

To apply by hand instead, run the same two commands as the playbook from `infra/`, infrastructure first. Nothing waits for the rollout or checks the result:

```bash
kubectl --context homelab apply -k k8s/infrastructure
kubectl --context homelab apply -k k8s/apps
```

`kubectl` is in `ansible/.venv/bin` after `make deps`. Preview with `kubectl kustomize k8s/apps` or `kubectl --context homelab diff -k k8s/apps`.

The `local-path-retain` StorageClass is not here: k3s deploys it on start from `ansible/manifests/`. Don't redefine it in this tree, or two things will own it.

## Plain-manifest apps

### nginx

The reverse proxy for the public hostname. A single `nginx:1.28-alpine` replica in namespace `ember` behind ClusterIP Service `nginx`. It is the only thing the public Ingress points at, so every request to `https://ember.tail470a31.ts.net` arrives here and nginx decides which Service gets it:

| Request | Where it goes |
|---|---|
| `POST /webhook/github` | Service `github-webhook` on port 8080 (next section). Other methods on this path are refused |
| `GET /healthz` | Answered by nginx itself with `ok`. It exists for the Deployment's probes and `make apps-health`, and does not depend on any other Service |
| Anything else | 404 |

The config is `apps/nginx/default.conf.template`. Kustomize ships it as a ConfigMap whose name ends in a hash of the file's contents, so an edit changes the name and the next `make apps-up` rolls the Deployment onto the new config by itself. If nginx cannot load the edited file, the new pod fails to start, the old pod keeps serving, and `apps-up` fails while waiting for the rollout. The file is a template because the image fills in `${NGINX_LOCAL_RESOLVERS}` (the cluster's DNS server) when the container starts; leave that placeholder as it is.

To add a route, copy the `location = /webhook/github` block and change the path and the Service it proxies to. Keep the Service's address in a variable, as that block does (`set $github_webhook github-webhook.ember.svc.cluster.local:8080;` then `proxy_pass http://$github_webhook;`). With a variable, nginx looks the Service up when a request arrives instead of when it starts, so nginx starts and stays up even when that Service is absent. With the name written straight into `proxy_pass`, nginx would refuse to start until the Service exists.

The webhook route accepts request bodies up to 8 MiB (`client_max_body_size 8m`). That matches the real receiver's limit (`MAX_BODY_BYTES` in `services/github-ingestion/envelope.py`), so nginx and the receiver agree on which deliveries are too large. A route that does not set its own cap gets nginx's default of 1 MiB. nginx passes the body and GitHub's headers (`X-GitHub-*`, `X-Hub-Signature-256`) through unchanged, so a signature check behind it sees exactly what GitHub sent.

`apps/nginx/ingress.yaml` publishes nginx on the public internet through the Tailscale operator and Funnel (below). The hostname comes from `tls.hosts: [ember]` and is what the GitHub App registers as its callback and webhook URL, so it must not change. Add new backends as routes in nginx and leave the Ingress pointing at nginx. Nothing handles the App's callback URL yet, so that path returns 404 like any other; when something does, it is one more `location`.

### github-webhook

A placeholder listener, not the real GitHub ingestion worker. It is there to prove that a delivery from GitHub reaches a container on the cluster before the worker is deployed (EMBER-56). One `python:3.12-slim` replica in namespace `ember` behind ClusterIP Service `github-webhook` on port 8080.

On `POST /webhook/github` it reads the body, discards it, and returns 200 with `{"status": "ok", "listener": "placeholder"}`. It logs one line per delivery with the event name, the delivery id, the body size and whether a signature header was present. It stores nothing and never logs the payload.

It does not verify signatures, so anyone on the internet can POST to it. That is acceptable only because it stores nothing; the real worker has to check the signature.

The code is `apps/github-webhook/listener.py`. The cluster has no image registry, so the file is shipped as a ConfigMap and mounted into a stock `python:3.12-slim` image instead of being built into one. As with nginx, the ConfigMap's name carries a content hash, so an edit to `listener.py` rolls the Deployment on the next `make apps-up`.

To see deliveries:

```bash
kubectl --context homelab -n ember logs deploy/github-webhook
```

Lines with the delivery id `apps-health` are the test deliveries that `make apps-health` sends.

To watch a real delivery arrive, set the GitHub App's webhook URL to `https://ember.tail470a31.ts.net/webhook/github`. GitHub then sends a `ping`, which shows up in those logs. Do this for that one `ping` only. The placeholder answers 200 and there is no secret, so GitHub records every delivery as successful while the placeholder throws it away. Once you have seen the `ping`, clear the webhook URL (or subscribe the App to no events) until the real worker is in.

Service `github-webhook` on port 8080 is the stable name nginx proxies to. EMBER-56 replaces what runs behind it with the real receiver from `services/github-ingestion`, with the webhook secret and the signature check, and does not touch the proxy. Three things to know when doing that:

- `kubectl apply -k` does not delete an object because it was removed from the manifests. If you add a new Deployment whose pods are labelled `app: github-webhook` and only take the placeholder out of the kustomization, the placeholder keeps running, Service `github-webhook` splits requests between the two, and about half the deliveries get a 200 and are discarded. Either replace the pod template of Deployment `github-webhook` in place (a Deployment's selector cannot be changed, so keep the label `app: github-webhook`), or delete the placeholder Deployment explicitly when you add the new one.
- The webhook checks in `make apps-health` look for `"listener": "placeholder"` in the response. Update them in `ansible/playbooks/apps.yml` when the real worker goes in.
- A kustomize generator under `infra/k8s/apps/` cannot read `services/github-ingestion/*.py`, because kustomize only reads files under its own directory. How the real worker's code or image reaches the cluster is still to be decided.

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
| `make apps-health` fails on the hostname check | The Tailscale device was recreated as `ember-1`, which is not the hostname GitHub posts to. Remove the stale `ember` device in the Tailscale admin console, then `make apps-down` and `make apps-up` |
| After a config edit, the new nginx pod crash-loops while the old one keeps serving | nginx could not load the edited config. `kubectl --context homelab -n ember get pods -l app=nginx` names the pod that is restarting, and `kubectl --context homelab -n ember logs <that pod>` shows the line nginx rejected. Fix `apps/nginx/default.conf.template` and run `make apps-up` again |

## Tailscale operator

Serves Ingresses with `ingressClassName: tailscale`. Each one gets a proxy that joins the tailnet as its own device named after `tls.hosts`, with a Let's Encrypt certificate for `https://<name>.tail470a31.ts.net`. The `tailscale.com/funnel: "true"` annotation also opens it to the internet. Values in `infrastructure/tailscale-operator/values.yaml`; chart version pinned in `ansible/playbooks/tailscale-operator.yml`.

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
3. **Settings > Trust credentials**: Credential > OAuth, Read and Write on Devices > Core, Keys > Auth Keys and General > Services, tag `tag:k8s-operator`. Store the ID and secret from `infra/ansible` with `.venv/bin/ansible-vault edit group_vars/all/vault.yml` as `vault_ts_oauth_client_id` and `vault_ts_oauth_client_secret`.

Run from `infra/ansible`:

| Target | What it does |
|---|---|
| `make ts-operator-up` | Creates Secret `operator-oauth` from the vault, installs the chart, then health. Safe to re-run |
| `make ts-operator-health` | Operator ready and logged in; each Tailscale Ingress has a hostname and answers over HTTPS; each Funnel Ingress also has a public DNS record |
| `make ts-operator-down` | Uninstalls the operator and removes the Secret. Refuses while a Tailscale Ingress exists (`make apps-down` first); CRDs stay |

A changed OAuth secret only takes effect after `kubectl --context homelab -n tailscale rollout restart deploy/operator`.

Requests from a tailnet device resolve the name through MagicDNS and go straight to the proxy, so they work whether or not Funnel does. Test Funnel from a device off the tailnet, or rely on the public DNS check in `ts-operator-health`.

| Symptom | Check |
|---|---|
| The URL loads on tailnet devices, everything else gets a DNS error | The proxy has no `funnel` attribute, so the name was never published. Add the `nodeAttrs` entry above; the record appears within a minute or two. Resolvers cache the earlier "not found" for up to 5 minutes |
| Still no public record after the policy change | `kubectl --context homelab -n tailscale rollout restart statefulset -l tailscale.com/parent-resource=ember` |
| `ts-operator-up` fails on "Check the operator logged in" | The OAuth credential lacks a scope or the `tag:k8s-operator` tag |
| First request hangs for up to a minute | The proxy is fetching its Let's Encrypt certificate; later requests are fast |

## Helm charts (until Flux)

Ansible installs charts with the `helm` CLI from a committed `values.yaml`, so each one moves into a Flux `HelmRelease` as-is later. The chart version is pinned in the playbook so a reinstall doesn't pick up a newer chart.

### Neo4j

Community edition, release `neo4j` in namespace `ember`, pinned to hv-worker-1 with a 10Gi `local-path-retain` volume. `apps/neo4j/service-lb.yaml` publishes it on every node's IP through k3s ServiceLB, tailnet sources only. Run from `infra/ansible` (`make deps` first for helm):

| Target | What it does |
|---|---|
| `make neo4j-prep` | Checks helm, the pinned node, and the StorageClass; applies the namespace; adds the chart repo |
| `make neo4j-up` | prep, then `helm upgrade --install` and the node-IP Service, then health. Safe to re-run after editing `values.yaml` |
| `make neo4j-health` | Pod ready on hv-worker-1, `RETURN 1` inside the pod, each node's `<node>.tail470a31.ts.net` name resolves to its IP, and an authenticated query on port 7474 of every node |
| `make neo4j-down` | Removes the Service and uninstalls the release. The data volume stays, and the next `neo4j-up` reattaches it |

Connect with user `neo4j`:

- From the tailnet: `bolt://hv-worker-1.tail470a31.ts.net:7687`, Browser at `http://hv-worker-1.tail470a31.ts.net:7474`. The full name resolves on any tailnet device, including ones that don't use the tailnet's search domain. `pi-cp.tail470a31.ts.net` and `hv-worker-2.tail470a31.ts.net` work too, since ServiceLB forwards from every node. Use `bolt://`, not `neo4j://`: the routing scheme reconnects to the server's advertised address, which isn't set to the node's name.
- In-cluster: `bolt://neo4j.ember.svc.cluster.local:7687`.

To wipe the data after `neo4j-down`, delete PVC `data-neo4j-0`, its PV, and the PV's directory under `/var/lib/rancher/k3s/storage` on hv-worker-1.

## Flux

The layout follows Flux's `infrastructure/` + `apps/` convention, so bootstrapping should only add `clusters/homelab/` with two Flux `Kustomization`s pointing at these paths (apps `dependsOn` infrastructure). The manifests themselves shouldn't need to move.
