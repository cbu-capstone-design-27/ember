# Container images

CI builds a container image for every service that has a Dockerfile, and publishes it to the GitHub Container Registry (GHCR) when `main` moves. Jira: EMBER-42.

## What the pipeline does

The `image targets` and `image (<name>)` jobs in `.github/workflows/ci.yml` handle this.

- **Targets.** Every `services/<name>/Dockerfile` and `apps/<name>/Dockerfile` is a target. The image is named after the directory.
- **Every PR and push** to `develop` or `main` builds each target for `linux/amd64` (hv-workers) and `linux/arm64` (pi-cp). A broken Dockerfile fails `ci-ok`.
- **A push to `main`** also publishes each image to `ghcr.io/cbu-capstone-design-27/ember/<name>` with two tags:
  - `sha-<short sha>`: immutable. Deploy this one.
  - `latest`: moves with `main`.

Publishing uses the workflow's `GITHUB_TOKEN`, so no secrets need to be set up. `develop` builds but never publishes.

Today's targets are `github-ingestion`, `jira-ingestion` and `slack-ingestion`.

## Adding a new service

1. Create `services/<name>/Dockerfile`, or `apps/<name>/Dockerfile` for a user-facing app.
   - `<name>` must be lowercase and unique across `services/` and `apps/`. The `image targets` job fails on a collision.
   - The build context is the **repo root**, so paths in `COPY` start with `services/<name>/...`. That lets a Dockerfile pull in `packages/` later.
   - Follow the existing receivers: a slim base image, `USER nobody`, and config through environment variables. `services/github-ingestion/Dockerfile` is the smallest example.
2. Check that the image builds locally from the repo root:

   ```sh
   docker build -f services/<name>/Dockerfile -t ember/<name>:dev .
   ```

3. Optional: add a profile to `infra/docker-compose.yml` with `build.context: ..`, the same way `github-ingestion` does.
4. Open a PR. An `image (<name>)` job shows up next to the others, and it must pass for `ci-ok` to go green. You don't edit the workflow.

The image is published on the next merge to `main`.

## Pulling images

```sh
docker pull ghcr.io/cbu-capstone-design-27/ember/<name>:sha-<short sha>
```

Each package is linked to this repo through its `org.opencontainers.image.source` label. After a package's **first** publish, check its visibility under the organization's Packages tab:

- **Public:** the k3s cluster can pull it as-is.
- **Private:** a pull needs a token with `read:packages`, and the cluster needs an `imagePullSecret`.

## Removing a service

Delete its Dockerfile, or the whole directory. CI stops building it. Images already published stay in GHCR until someone deletes the package.
