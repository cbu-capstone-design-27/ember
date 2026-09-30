# Run from infra/ansible. Everything uses the local .venv, so no system Ansible is needed.
PYTHON  ?= python3
VENV    := .venv
BIN     := $(abspath $(VENV))/bin
export PATH := $(BIN):$(PATH)
export ANSIBLE_CONFIG := $(abspath ansible.cfg)

K3S_VERSION := $(shell sed -n 's/^k3s_version: *//p' group_vars/all/main.yml)
KUBECTL_VERSION := $(firstword $(subst +, ,$(K3S_VERSION)))
HELM_VERSION := v4.3.0
CONTEXT := homelab

.PHONY: help deps vault-init ping prep up nodes smoke kubeconfig upgrade reboot reset \
	apps-diff apps-up apps-down neo4j-prep neo4j-up neo4j-health neo4j-down

help:
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'

deps: ## Create .venv with Ansible, install the pinned collections, kubectl and helm
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

apps-diff: ## Preview what apps-up would change on the cluster
	kubectl --context $(CONTEXT) diff -k ../k8s/infrastructure || [ $$? -eq 1 ]
	kubectl --context $(CONTEXT) diff -k ../k8s/apps || [ $$? -eq 1 ]

# Every app lives in namespace ember, so rollout status runs there.
apps-up: ## Apply k8s/infrastructure then k8s/apps, and wait for their workloads to roll out
	kubectl --context $(CONTEXT) apply -k ../k8s/infrastructure
	kubectl --context $(CONTEXT) apply -k ../k8s/apps
	kubectl kustomize ../k8s/apps | kubectl --context $(CONTEXT) get -f - -o name \
	  | grep -E '^(deployment|statefulset|daemonset)\.' \
	  | xargs -r -n1 kubectl --context $(CONTEXT) -n ember rollout status --timeout=180s

# Leaves k8s/infrastructure alone: deleting the ember namespace would also take Neo4j with it.
apps-down: ## Delete everything in k8s/apps (keeps the namespace and Neo4j)
	kubectl --context $(CONTEXT) delete -k ../k8s/apps --ignore-not-found --wait

neo4j-prep: ## Neo4j prerequisites: helm, pinned node Ready, StorageClass, namespace, chart repo
	ansible-playbook playbooks/neo4j.yml --tags prep

neo4j-up: ## Install/upgrade Neo4j from k8s/apps/neo4j, publish it on node IPs, then health check
	ansible-playbook playbooks/neo4j.yml --tags prep,up,health

neo4j-health: ## Neo4j pod ready on its node, answers a query in-pod and on every node's IP
	ansible-playbook playbooks/neo4j.yml --tags health

neo4j-down: ## Uninstall Neo4j and its node-IP Service (keeps the data volume)
	ansible-playbook playbooks/neo4j.yml --tags down
