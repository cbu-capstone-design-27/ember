# Vertex AI cutover and tunnel retirement

Jira: [EMBER-155](https://ember-capstone.atlassian.net/browse/EMBER-155).
Ryan's embedding implementation: EMBER-116. Graphiti reference: EMBER-146.

Status: the local Graphiti test accepts native Vertex AI. Production cutover and tunnel retirement are pending.
Coordinate the cluster operations with Jacob. The owning Flux Kustomization already has `spec.prune: true` in
`infra/k8s/clusters/homelab/apps.yaml`; keep it enabled throughout the cutover.

## Validate the replacement first

1. Record the approved project, region, Gemini model, embedding model and dimensions. Establish renewable Google
   credentials for each workload; store credentials through the existing encrypted Secret process. Agree quotas
   and billing ownership. Do not commit keys or tokens.
2. Run the [small Graphiti sample](graphiti-test.md#the-real-run-on-vertex-ai-ember-155) on synthetic photon data.
   Inspect extraction failures, ranking results, reranker failures, API errors and usage. Then run the full reference
   on a fresh local graph. Save the selected model IDs and report, with quality results clearly separate from
   simulated-response compatibility tests.
3. Integrate EMBER-116's embedding provider with the worker and MCP query path. Treat a model change as a new
   embedding space even when dimensions stay the same. Plan the re-embedding, index rebuild, model identity and
   cutover together. Pause retrieval or use isolated columns/indexes until the new corpus is complete. Document
   how to roll back to a matching provider and corpus, rather than mixing old documents with new query vectors.
4. Validate the deployed worker and MCP against photon questions, tenant/source filtering and current-record
   rules. Validate every LLM consumer against Vertex, including any implementation of EMBER-106. Inspect quota,
   transient-error and permanent-error handling. Capture results before retiring the fallback.

## Remove the tunnel through GitOps

Before deleting anything, inventory references:

    rg -n 'llm-tunnel|spark-model-keys|LLM_API_KEY|EMBEDDING_API_KEY' infra services apps config .github Makefile .env.example

In the current checkout, `infra/k8s/apps/embed/deployment.yaml` still uses `http://llm-tunnel.ember:8000` and
`spark-model-keys`. Those must be replaced by the validated Vertex configuration before the Secret is removed.
Archived research can retain historical descriptions, with current operating instructions pointing to Vertex.

Using metadata only (never dumping Secret data), check live resources for the annotation or label
`kustomize.toolkit.fluxcd.io/prune: disabled`, and check the Flux inventory. If an exclusion exists, remove it
through the owning manifests and reconcile that change **before** removing the manifests from Git.
Check both the owning Flux Kustomization and the resources; setting `prune` on an ordinary Kustomize file does
not configure Flux garbage collection.

After replacement consumers are healthy, remove the `llm-tunnel` resource entry from
`infra/k8s/apps/kustomization.yaml`, its manifest directory, obsolete tunnel container build/workflow entries,
Ansible setup and Makefile targets. Coordinate removal of its dedicated Spark SSH authorization and model
credentials with Jacob. Do not remove the `ember` namespace, shared credentials or unrelated workloads.

After the change is merged and visible in Flux's source revision, Jacob can reconcile:

    flux reconcile kustomization apps -n flux-system --with-source
    flux get kustomization apps -n flux-system

Record the applied revision and Ready state. Check these resources return **NotFound**, distinguishing that
result from an authorization or connection failure:

    kubectl get deployment llm-tunnel -n ember
    kubectl get service llm-tunnel -n ember
    kubectl get secret llm-tunnel-ssh -n ember
    kubectl get secret spark-model-keys -n ember

List ConfigMap **names** and confirm none begin with `llm-tunnel-known-hosts`, including generated hash suffixes:

    kubectl get configmap -n ember -o name

Also check that no tunnel pods remain, replacement workloads are healthy, and the owning Flux inventory has no
tunnel entries. Attach that evidence to EMBER-155. Deleting YAML, a successful CI build, or `prune: true` alone
does not establish that live resources disappeared. Keep EMBER-155 open until these checks and the real Vertex
validation pass.
