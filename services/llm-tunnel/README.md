# llm-tunnel

An SSH tunnel from the cluster to the chat model on the DGX Spark (EMBER-49, ADR 0008).

The chat model (Qwen3-30B-A3B) listens on the Spark's loopback, `127.0.0.1:8001`, with no public address: the Spark is shared, can't join Tailscale, and keeps one public port (8000, embeddings). This image runs `ssh -N -L 0.0.0.0:8001:127.0.0.1:8001` as user `save-water` on the Spark, so pods reach the model at `http://llm-tunnel.ember:8001/v1` (`LLM_BASE_URL`). The model still checks its own API key on every request.

The image is only Alpine's OpenSSH client, with `ssh` as the entrypoint. Deployment `llm-tunnel` passes every option, the key and the pinned host key (`infra/k8s/README.md`, llm-tunnel). `ssh` exits when the connection dies (`ServerAliveInterval`) or the forward can't be set up (`ExitOnForwardFailure`), and Kubernetes restarts it.

To try it outside the cluster, with the key and host key in `./ssh`:

```sh
docker build -f services/llm-tunnel/Dockerfile -t ember/llm-tunnel:dev .
docker run --rm -p 8001:8001 -v "$PWD/ssh:/etc/llm-tunnel:ro" ember/llm-tunnel:dev \
  -N -F /dev/null -i /etc/llm-tunnel/id_ed25519 -o UserKnownHostsFile=/etc/llm-tunnel/known_hosts \
  -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -L 0.0.0.0:8001:127.0.0.1:8001 \
  save-water@172.99.171.11
```
