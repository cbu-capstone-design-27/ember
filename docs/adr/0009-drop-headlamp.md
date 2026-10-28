# ADR 0009: Drop Headlamp; Grafana Cloud is the cluster UI

- Status: Accepted
- Date: 2026-10-28
- Jira: EMBER-64

## Context

[ADR 0003](0003-gitops-controller.md) chose Flux, which has no web UI, and named Headlamp's Flux plugin as the way to get one. We ran Headlamp with that plugin in namespace `headlamp`, on the tailnet at `https://headlamp.tail470a31.ts.net`, signed in with a 24-hour read-only token from `make headlamp-token`.

EMBER-64 was opened because the dashboard stopped working. Since then, EMBER-91 sent the cluster's metrics, logs and events to Grafana Cloud (`infra/k8s/README.md`, Monitoring). That covers what we used Headlamp for:

- **Workloads.** Nodes, pods and workloads from the kubelet, cAdvisor and kube-state-metrics, on dashboard **Ember** and in the Kubernetes Monitoring app.
- **Flux.** kube-state-metrics exports Ready state for Flux's Kustomizations, HelmReleases and sources as `gotk_resource_info`, Flux's documented setup, and dashboard **Ember** shows it.
- **Logs and events.** Every pod's log and the cluster's Kubernetes events go to Loki.

Grafana Cloud also keeps 14 days of history and works when the cluster is down or being moved. Headlamp showed only the live state, and only from the tailnet.

## Decision

Remove Headlamp from the cluster. Grafana Cloud is the browser view of the cluster and of Flux. The `flux` CLI (`make flux-health`, `flux get all -A`) stays the way to see and act on Flux state from a terminal.

The removal goes through Git in two steps, because every namespace in `infrastructure/namespaces.yaml` carries `kustomize.toolkit.fluxcd.io/prune: disabled` and Flux reads that annotation from the live object:

1. Delete `apps/headlamp/` and drop the annotation from the `headlamp` Namespace.
2. After Flux has applied step 1, delete the Namespace, which Flux then prunes.

## Consequences

- One fewer release, Ingress and tailnet device on the two small workers.
- Grafana Cloud shows Flux's Ready state and history but cannot act. Reconciling, suspending and resuming stay with `make sync` and the `flux` CLI, as they did with Headlamp's read-only viewer.
- Looking at the cluster in a browser now needs a Grafana Cloud login rather than tailnet access. Grafana Cloud does not show Secrets, and neither did Headlamp's viewer.
- If an interactive cluster UI is needed again, ADR 0003's options (Headlamp's Flux plugin, Capacitor) still apply; reinstating one is a new ADR.
