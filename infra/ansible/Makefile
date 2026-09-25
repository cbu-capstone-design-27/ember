# Run from infra/ansible. Everything uses the local .venv, so no system Ansible is needed.
VENV    := .venv
BIN     := $(abspath $(VENV))/bin
export PATH := $(BIN):$(PATH)
export ANSIBLE_CONFIG := $(abspath ansible.cfg)

K3S_VERSION := $(shell sed -n 's/^k3s_version: *//p' group_vars/all/main.yml)
KUBECTL_VERSION := $(firstword $(subst +, ,$(K3S_VERSION)))
CONTEXT := homelab

.PHONY: help deps vault-init ping prep up nodes smoke kubeconfig upgrade reboot reset

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

deps: ## Create .venv with Ansible, install the pinned collections and kubectl
	test -x $(BIN)/python || python3 -m venv $(VENV)
	$(BIN)/pip install -q -r requirements.txt
	$(BIN)/ansible-galaxy collection install -r requirements.yml -p collections
	@if ! $(BIN)/kubectl version --client 2>/dev/null | grep -q '$(KUBECTL_VERSION)'; then \
	  curl -fsSLo $(BIN)/kubectl https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/linux/amd64/kubectl && \
	  curl -fsSL https://dl.k8s.io/release/$(KUBECTL_VERSION)/bin/linux/amd64/kubectl.sha256 | \
	    sed 's|$$|  $(BIN)/kubectl|' | sha256sum -c - && \
	  chmod +x $(BIN)/kubectl; \
	fi
	$(BIN)/kubectl version --client

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
	ansible-playbook k3s.orchestration.site --tags kubeconfig

upgrade: ## Upgrade k3s to k3s_version in group_vars/all/main.yml (then make deps for a matching kubectl)
	ansible-playbook k3s.orchestration.upgrade

reboot: ## Rolling reboot of every node
	ansible-playbook k3s.orchestration.reboot

reset: ## Uninstall k3s from every node (destroys the cluster and local-path data)
	@read -p "Type 'reset homelab' to uninstall k3s from all nodes: " ans && [ "$$ans" = "reset homelab" ]
	ansible-playbook k3s.orchestration.reset
