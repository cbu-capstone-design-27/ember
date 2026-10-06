# ADR 0003: GitOps controller for the homelab cluster

- Status: Accepted
- Date: 2026-10-06
- Jira: EMBER-57

## Context

Deploys to the `homelab` k3s cluster are pushed by hand from a controller (WSL) through Ansible, in several separate ways:

- `make apps-up` runs `kubectl apply -k` on `infra/k8s/infrastructure` and `infra/k8s/apps`.
- `make neo4j-up` and `make ts-operator-up` run `helm upgrade --install` with a chart version pinned in the playbook, then apply extra objects. Neo4j's Service is left out of `apps/kustomization.yaml` so `apps-up` does not create it before the release exists.
- k3s applies `local-path-retain` itself from `ansible/manifests/`.

Ordering lives in people's heads (`ts-operator-up` before the first `apps-up`). `kubectl apply` never deletes what was removed from Git, which is why `apps-down` deletes ConfigMaps by the `ember.io/generated` label and why `infra/k8s/README.md` warns that the placeholder `github-webhook` keeps running when the real receiver is added. Nothing shows whether the cluster matches Git, and only someone with `.vault-pass` and the kubeconfig can deploy.

EMBER-42 now publishes every service image to GHCR, but nothing on the cluster consumes them.

The cluster is a Raspberry Pi 5 control plane (pi-cp, tainted `CriticalAddonsOnly`) and two Hyper-V workers with about 4 GB each. Neo4j already requests 2 Gi on hv-worker-1. Anything we add runs on the two workers.

We compared Flux and Argo CD. Both pull from Git, so neither needs a route into the cluster.

## Decision

Use **Flux** to reconcile the cluster from this repo.

- **Footprint.** Flux is a few single-purpose controllers. Argo CD runs a server, repo-server, application controller, Redis, ApplicationSet controller, Dex and notifications, which is a real cost on two small workers.
- **Layout.** `infra/k8s/` already follows Flux's `infrastructure/` + `apps/` convention, and the Helm `values.yaml` files were written to become `HelmRelease`s. Adopting Flux adds `infra/k8s/clusters/homelab/`; the existing manifests stay where they are.
- **Secrets.** Flux decrypts SOPS-encrypted Secrets natively. The Tailscale OAuth Secret, the Neo4j password and the GHCR pull secret can live in Git encrypted with one age key, and that key is the only secret Ansible Vault still has to deliver to the cluster. Argo CD needs Sealed Secrets, External Secrets or a plugin for the same thing.
- **Helm.** Flux's helm-controller runs real Helm releases, so it can take over the `neo4j` and `tailscale-operator` releases Ansible installed, and `helm history` keeps working. Argo CD renders charts and applies the objects, which would leave the existing release metadata stale.
- **Images.** Flux's image-reflector and image-automation controllers can follow the GHCR tags from EMBER-42 and commit tag bumps to Git. Argo CD needs Argo CD Image Updater, a separate project.
- **Pruning.** Flux deletes what is removed from Git, which retires the `ember.io/generated` cleanup and the placeholder trap above. Argo CD can prune too; this is not a differentiator, but it is a reason to move to either.

## Consequences

- No built-in web UI. Argo CD's UI would help demos and onboarding. If we need one, Headlamp's Flux plugin or Capacitor reads Flux's objects without running Argo CD. Day-to-day status is `flux get all -A`.
- Ansible keeps node prep, k3s install, installing Flux, and creating the age key Secret. It stops applying manifests and Helm charts. `make apps-health` and the other end-to-end checks stay, because Flux cannot check the public route through Funnel.
- Image tags must sort. `develop-<short sha>` has no order, so CI adds a tag with the run number (for example `develop-123-abc1234`) for Flux's `ImagePolicy` to pick the newest.
- Image automation pushes commits. Branch protection on `develop` and `main` (required `ci-ok`) has to allow that, or the automation pushes to its own branch and changes arrive as PRs. That choice is made when image automation is turned on.
- The embedding service on the DGX Spark is outside the cluster and keeps `infra/spark/deploy.sh`. Local development keeps `infra/docker-compose.yml`.
