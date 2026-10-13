# Container images

CI builds a container image for every service that has a Dockerfile, and publishes it to the GitHub Container Registry (GHCR) when `main` or `develop` moves. Jira: EMBER-42.

## What the pipeline does

The `image targets` and `image (<name>)` jobs in `.github/workflows/ci.yml` handle this.

- **Targets.** Every `services/<name>/Dockerfile` and `apps/<name>/Dockerfile` is a target. The image is named after the directory.
- **Every PR and push** to `develop` or `main` builds each target for `linux/amd64` (hv-workers) and `linux/arm64` (pi-cp). A broken Dockerfile fails `ci-ok`.
- **A push to `main` or `develop`** also publishes each image to `ghcr.io/cbu-capstone-design-27/ember/<name>`. Each branch gets its own tags, so the two lines can run side by side on different hv-workers (for example, a staging environment on `main` and a development environment on `develop`):

  | Branch    | Pinned tags (immutable)                          | Moving tags        |
  |-----------|--------------------------------------------------|--------------------|
  | `main`    | `main-<short sha>`, `main-<run>-<short sha>`       | `main`, `latest`   |
  | `develop` | `develop-<short sha>`, `develop-<run>-<short sha>` | `develop`          |

  Deploy a pinned tag. Use a moving tag only where "whatever the branch last built" is what you want. `<run>` is the CI workflow's run number, which only goes up, so `<branch>-<run>-<short sha>` tags sort by build order. Flux uses that to pick the newest image (ADR 0003); `<branch>-<short sha>` tags cannot be sorted.
- **PRs** build an image tagged `pr-<n>`, but never push it.

Publishing uses the workflow's `GITHUB_TOKEN`, so no secrets need to be set up.

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

The image is published as soon as the PR merges into `develop`, and again when `develop` is merged to `main`.

## Pulling images

```sh
docker pull ghcr.io/cbu-capstone-design-27/ember/<name>:develop-<short sha>
```

Each package is linked to this repo through its `org.opencontainers.image.source` label.

**The packages are private**, even though this repo is public, and they stay that way on purpose. Any pull needs a token with `read:packages`:

- **Locally:** `gh auth refresh -s read:packages`, then `gh auth token | docker login ghcr.io -u <github user> --password-stdin`.
- **On the cluster:** an `imagePullSecret` for `ghcr.io` in each namespace that runs these images. Keep the token in Ansible Vault (`infra/ansible/group_vars/all/vault.yml`, alongside the other secrets) and use a token that can only read packages.

A new service's package is private from its first publish, so it needs no extra step. Without credentials, a pull fails with `authentication required`.

Each published tag is a multi-arch index with `linux/amd64` and `linux/arm64` images. The registry also lists two `unknown/unknown` entries: those are build provenance attestations, not images.

## Removing a service

Delete its Dockerfile, or the whole directory. CI stops building it. Images already published stay in GHCR until someone deletes the package.
