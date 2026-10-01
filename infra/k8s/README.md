# k8s

Manifests for the `homelab` k3s cluster. Node and k3s setup live in `../ansible` (see `../docs/k3-setup.md`).

| Directory | What goes in it |
|---|---|
| `infrastructure/` | Cluster-wide pieces: namespaces, CRDs, operators, ingress, cert-manager |
| `apps/` | Ember workloads, one subdirectory per app. Plain manifests are listed in `apps/kustomization.yaml`; Helm apps hold a `values.yaml` instead (see below) |

Each directory is a kustomize root. Until Flux is in place, apply by hand from `infra/`, infrastructure first:

```bash
kubectl --context homelab apply -k k8s/infrastructure
kubectl --context homelab apply -k k8s/apps
```

Or from `infra/ansible`: `make apps-diff` to preview, then `make apps-up`, which applies both and waits for the workloads to roll out. `make apps-down` deletes what's in `apps/` and leaves `infrastructure/` in place.

`kubectl` is in `ansible/.venv/bin` after `make deps`. Preview with `kubectl kustomize k8s/apps` or `kubectl --context homelab diff -k k8s/apps`.

The `local-path-retain` StorageClass is not here: k3s deploys it on start from `ansible/manifests/`. Don't redefine it in this tree, or two things will own it.

## Plain-manifest apps

### nginx

A single `nginx:1.28-alpine` replica in namespace `ember`, with a ClusterIP Service reachable in-cluster at `http://nginx.ember.svc.cluster.local`. It is a starting point for plain-manifest apps rather than an Ember service. Deployed by `make apps-up`, removed by `make apps-down`.

`apps/nginx/ingress.yaml` publishes it on the public internet at `https://ember.tail470a31.ts.net` through the Tailscale operator and Funnel (below). That hostname is what the GitHub App registers as its callback and webhook URL, so when the real backend replaces nginx, repoint the Ingress's backend and keep `tls.hosts: [ember]`.

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
