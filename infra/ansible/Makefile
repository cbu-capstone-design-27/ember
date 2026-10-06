# Run from infra/ansible. Everything uses the local .venv, so no system Ansible is needed.
PYTHON  ?= python3
VENV    := .venv
BIN     := $(abspath $(VENV))/bin
export PATH := $(BIN):$(PATH)
export ANSIBLE_CONFIG := $(abspath ansible.cfg)

K3S_VERSION := $(shell sed -n 's/^k3s_version: *//p' group_vars/all/main.yml)
KUBECTL_VERSION := $(firstword $(subst +, ,$(K3S_VERSION)))
HELM_VERSION := v4.3.0
FLUX_VERSION := 2.9.6
SOPS_VERSION := v3.13.3
AGE_VERSION := v1.3.2
# age publishes no checksum file; this is the sha256 of age-$(AGE_VERSION)-linux-amd64.tar.gz
AGE_SHA256 := cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10
CONTEXT := homelab

.PHONY: help deps vault-init ping prep up nodes smoke kubeconfig upgrade reboot reset \
	flux-up flux-health flux-preview flux-preview-end flux-components sync apps-diff apps-health \
	ts-operator-health neo4j-health headlamp-token

help:
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

deps: ## Create .venv with Ansible, install the pinned collections, kubectl, helm, flux, sops and age
	test -x $(BIN)/python || $(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q -r requirements.txt
	$(BIN)/ansible-galaxy collection install -r requirements.yml -p collections
	@if ! $(BIN)/kubectl version --client 2>/dev/null | grep -q '$(KUBECTL_VERSION)'; then \
	  curl -fsSLo $(BIN)/kubectl https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/linux/amd64/kubectl && \
	  curl -fsSL https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/linux/amd64/kubectl.sha256 | \
	    sed 's|$$|  $(BIN)/kubectl|' | sha256sum -c - && \
	  chmod +x $(BIN)/kubectl; \
	fi
	$(BIN)/kubectl version --client
	@if ! $(BIN)/helm version --short 2>/dev/null | grep -q '^$(HELM_VERSION)+'; then \
	  tmp=$$(mktemp -d) && \
	  curl -fsSLo $$tmp/helm.tgz https://get.helm.sh/helm-$(HELM_VERSION)-linux-amd64.tar.gz && \
	  echo "$$(curl -fsSL https://get.helm.sh/helm-$(HELM_VERSION)-linux-amd64.tar.gz.sha256sum | cut -d' ' -f1)  $$tmp/helm.tgz" | sha256sum -c - && \
	  tar xzf $$tmp/helm.tgz -C $$tmp linux-amd64/helm && mv $$tmp/linux-amd64/helm $(BIN)/helm; \
	  rm -rf $$tmp; \
	fi
	$(BIN)/helm version --short
	@if ! $(BIN)/flux version --client 2>/dev/null | grep -q '$(FLUX_VERSION)$$'; then \
	  tmp=$$(mktemp -d) && \
	  curl -fsSLo $$tmp/flux.tgz https://github.com/fluxcd/flux2/releases/download/v$(FLUX_VERSION)/flux_$(FLUX_VERSION)_linux_amd64.tar.gz && \
	  echo "$$(curl -fsSL https://github.com/fluxcd/flux2/releases/download/v$(FLUX_VERSION)/flux_$(FLUX_VERSION)_checksums.txt | grep ' flux_$(FLUX_VERSION)_linux_amd64.tar.gz$$' | cut -d' ' -f1)  $$tmp/flux.tgz" | sha256sum -c - && \
	  tar xzf $$tmp/flux.tgz -C $$tmp flux && mv $$tmp/flux $(BIN)/flux; \
	  rm -rf $$tmp; \
	fi
	$(BIN)/flux version --client
	@if ! $(BIN)/sops --version 2>/dev/null | grep -q '$(patsubst v%,%,$(SOPS_VERSION))'; then \
	  tmp=$$(mktemp -d) && \
	  curl -fsSLo $$tmp/sops-$(SOPS_VERSION).linux.amd64 https://github.com/getsops/sops/releases/download/$(SOPS_VERSION)/sops-$(SOPS_VERSION).linux.amd64 && \
	  (cd $$tmp && curl -fsSL https://github.com/getsops/sops/releases/download/$(SOPS_VERSION)/sops-$(SOPS_VERSION).checksums.txt | grep ' sops-$(SOPS_VERSION).linux.amd64$$' | sha256sum -c -) && \
	  install -m 0755 $$tmp/sops-$(SOPS_VERSION).linux.amd64 $(BIN)/sops; \
	  rm -rf $$tmp; \
	fi
	$(BIN)/sops --version --disable-version-check
	@if ! $(BIN)/age --version 2>/dev/null | grep -q '^$(AGE_VERSION)$$'; then \
	  tmp=$$(mktemp -d) && \
	  curl -fsSLo $$tmp/age.tgz https://github.com/FiloSottile/age/releases/download/$(AGE_VERSION)/age-$(AGE_VERSION)-linux-amd64.tar.gz && \
	  echo "$(AGE_SHA256)  $$tmp/age.tgz" | sha256sum -c - && \
	  tar xzf $$tmp/age.tgz -C $$tmp age/age age/age-keygen && mv $$tmp/age/age $$tmp/age/age-keygen $(BIN)/; \
	  rm -rf $$tmp; \
	fi
	$(BIN)/age --version

vault-init: ## One time: create .vault-pass and the encrypted k3s join token
	@test ! -e .vault-pass || { echo ".vault-pass already exists; not overwriting" >&2; exit 1; }
	@test ! -e group_vars/all/vault.yml || { echo "group_vars/all/vault.yml already exists" >&2; exit 1; }
	@(umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > .vault-pass)
	@printf -- '---\nvault_k3s_token: %s\n' "$$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > group_vars/all/vault.yml
	@$(BIN)/ansible-vault encrypt group_vars/all/vault.yml
	@echo "Back up .vault-pass (password manager); without it the token in vault.yml is unreadable."

ping: ## Check Ansible can reach every node over the tailnet
	ansible k3s_cluster -m ansible.builtin.ping

prep: ## Node prep only (tailnet/hostname/memory checks, apt upgrade, time sync)
	ansible-playbook playbooks/prep.yml

up: ## Full bring-up: prep, k3s install, kubeconfig merge, smoke tests
	ansible-playbook site.yml

nodes: ## kubectl get nodes
	kubectl --context $(CONTEXT) get nodes -o wide

smoke: ## Smoke tests only (nodes, taint, DNS, PVC persistence)
	ansible-playbook playbooks/smoke.yml

kubeconfig: ## Re-fetch the kubeconfig into ~/.kube/config as context "homelab"
	ansible-playbook playbooks/kubeconfig.yml

upgrade: ## Upgrade k3s to k3s_version in group_vars/all/main.yml (then make deps for a matching kubectl)
	ansible-playbook k3s.orchestration.upgrade

reboot: ## Rolling reboot of every node
	ansible-playbook k3s.orchestration.reboot

reset: ## Uninstall k3s from every node (destroys the cluster and local-path data)
	@read -p "Type 'reset homelab' to uninstall k3s from all nodes: " ans && [ "$$ans" = "reset homelab" ]
	ansible-playbook k3s.orchestration.reset

flux-up: ## Install Flux and its age key; Flux then deploys k8s/ from develop by itself. Then health check
	ansible-playbook playbooks/flux.yml --tags prep,up,health

flux-health: ## Flux controllers healthy, every Kustomization applied; lists what is deployed
	ansible-playbook playbooks/flux.yml --tags health

flux-preview: ## Deploy a pushed branch instead of develop: make flux-preview BRANCH=<branch>
	ansible-playbook playbooks/flux.yml --tags preview -e preview_branch=$(BRANCH)

flux-preview-end: ## Go back to deploying develop after flux-preview
	ansible-playbook playbooks/flux.yml --tags preview-end

flux-components: ## Regenerate k8s/clusters/homelab/flux-system/gotk-components.yaml for FLUX_VERSION
	flux install --export > ../k8s/clusters/homelab/flux-system/gotk-components.yaml

sync: ## Make Flux fetch develop and apply it now instead of within the next minute
	flux --context $(CONTEXT) reconcile kustomization flux-system --with-source
	flux --context $(CONTEXT) reconcile kustomization infrastructure
	flux --context $(CONTEXT) reconcile kustomization apps

apps-diff: ## Show what this checkout's k8s/ would change on the cluster
	flux --context $(CONTEXT) diff kustomization infrastructure --path ../k8s/infrastructure || [ $$? -eq 1 ]
	flux --context $(CONTEXT) diff kustomization apps --path ../k8s/apps || [ $$? -eq 1 ]

apps-health: ## Apps rolled out; the GitHub webhook answers through nginx, from the tailnet and through Funnel
	ansible-playbook playbooks/apps.yml --tags health

ts-operator-health: ## Operator ready and logged in; every Tailscale Ingress has a hostname and answers HTTPS
	ansible-playbook playbooks/tailscale-operator.yml --tags health

neo4j-health: ## Neo4j pod ready on its node, answers a query in-pod and on every node's IP
	ansible-playbook playbooks/neo4j.yml --tags health

headlamp-token: ## Print a 24-hour sign-in token for Headlamp (read-only) at https://headlamp.<tailnet>
	@kubectl --context $(CONTEXT) -n headlamp create token headlamp-viewer --duration 24h
