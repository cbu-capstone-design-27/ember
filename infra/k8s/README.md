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

`kubectl` is in `ansible/.venv/bin` after `make deps`. Preview with `kubectl kustomize k8s/apps` or `kubectl --context homelab diff -k k8s/apps`.

The `local-path-retain` StorageClass is not here: k3s deploys it on start from `ansible/manifests/`. Don't redefine it in this tree, or two things will own it.

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

- From the tailnet: `bolt://hv-worker-1.tail470a31.ts.net:7687`, Browser at `http://hv-worker-1.tail470a31.ts.net:7474`. The full name resolves on any tailnet device, including ones that don't use the tailnet's search domain. `pi-cp.tail470a31.ts.net` works too, since ServiceLB forwards from every node. Use `bolt://`, not `neo4j://`: the routing scheme reconnects to the server's advertised address, which isn't set to the node's name.
- In-cluster: `bolt://neo4j.ember.svc.cluster.local:7687`.

To wipe the data after `neo4j-down`, delete PVC `data-neo4j-0`, its PV, and the PV's directory under `/var/lib/rancher/k3s/storage` on hv-worker-1.

## Flux

The layout follows Flux's `infrastructure/` + `apps/` convention, so bootstrapping should only add `clusters/homelab/` with two Flux `Kustomization`s pointing at these paths (apps `dependsOn` infrastructure). The manifests themselves shouldn't need to move.
