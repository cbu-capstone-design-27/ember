FROM alpine:3.22

# Only the OpenSSH client. The tunnel is `ssh -N -L ...`, run by Deployment llm-tunnel
# (infra/k8s/apps/llm-tunnel), which passes the host, key and forward as arguments. ssh exits when
# the connection dies (ServerAliveInterval) and Kubernetes restarts it, so no autossh.
RUN apk add --no-cache openssh-client-default

USER nobody
ENTRYPOINT ["ssh"]
