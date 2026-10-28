# Container images

CI builds a container image for every service that has a Dockerfile, and publishes it to the GitHub Container Registry (GHCR) when `main` or `develop` moves. Jira: EMBER-42.

## What the pipeline does

The `image targets` and `image (<name>)` jobs in `.github/workflows/ci.yml` handle this.

- **Targets.** Every `services/<name>/Dockerfile` and `apps/<name>/Dockerfile` is a target. The image is named after the directory.
- **Every PR and push** to `develop` or `main` builds each target for `linux/amd64` (hv-workers) and `linux/arm64` (pi-cp). A broken Dockerfile fails `ci-ok`.
- **Except `web`**, which builds only when `apps/web/` or `.github/workflows/ci.yml` changed (the `web` output of `detect changes`). A push to `main` or `develop` that leaves the web app alone publishes no new `web` tags, and the newest ones still point at the last web change.
- **A push to `main` or `develop`** also publishes each image to `ghcr.io/cbu-capstone-design-27/ember/<name>`. Each branch gets its own tags, so the two lines can run side by side on different hv-workers (for example, a staging environment on `main` and a development environment on `develop`):

  | Branch    | Pinned tags (immutable)                          | Moving tags        |
  |-----------|--------------------------------------------------|--------------------|
  | `main`    | `main-<short sha>`, `main-<run>-<short sha>`       | `main`, `latest`   |
  | `develop` | `develop-<short sha>`, `develop-<run>-<short sha>` | `develop`          |

  Deploy a pinned tag. Use a moving tag only where "whatever the branch last built" is what you want. `<run>` is the CI workflow's run number, which only goes up, so `<branch>-<run>-<short sha>` tags sort by build order. Flux uses that to pick the newest image (ADR 0003); `<branch>-<short sha>` tags cannot be sorted.
- **PRs** build an image tagged `pr-<n>`, but never push it.

Publishing uses the workflow's `GITHUB_TOKEN`, so no secrets need to be set up.

Today's targets are `github-ingestion`, `gitlab-ingestion`, `intake`, `jira-ingestion`, `llm-tunnel`, `pipeline`, `slack-ingestion` and `web`.

## Adding a new service

1. Create `services/<name>/Dockerfile`, or `apps/<name>/Dockerfile` for a user-facing app.
   - `<name>` must be lowercase and unique across `services/` and `apps/`. The `image targets` job fails on a collision.
   - The build context is the **repo root**, so paths in `COPY` start with `services/<name>/...`. That lets a Dockerfile pull in `packages/` later.
   - Follow the existing services: a slim base image, `USER nobody`, and config through environment variables. `services/jira-ingestion/Dockerfile` is the smallest example, and `services/intake/Dockerfile` adds a `requirements.txt`.
2. Check that the image builds locally from the repo root:

   ```sh
   docker build -f services/<name>/Dockerfile -t ember/<name>:dev .
   ```

3. Optional: add a profile to `infra/docker-compose.yml` with `build.context: ..`, the same way `intake` does.
4. Open a PR. An `image (<name>)` job shows up next to the others, and it must pass for `ci-ok` to go green. You don't edit the workflow.
5. Once the PR merges, make the new package public ([Pulling images](#pulling-images)).

The image is published as soon as the PR merges into `develop`, and again when `develop` is merged to `main`.

## Pulling images

```sh
docker pull ghcr.io/cbu-capstone-design-27/ember/<name>:develop-<short sha>
```

Each package is linked to this repo through its `org.opencontainers.image.source` label.

**The packages are public**, like this repo. Anyone can pull them without logging in, and the cluster needs no `imagePullSecret`. They hold nothing the repo doesn't: CI builds them from a clean checkout with no build secrets, and secrets reach a container only through its environment at run time. Keep it that way: never `COPY` a secret or bake one in with `ARG` or `ENV`.

**A new package is private on its first publish.** Make it public once, right after the PR that adds its Dockerfile merges: on the package's page on GitHub, open Package settings, then Change visibility under Danger Zone, and pick Public. GitHub has no API call for this, and it takes admin rights on the package. Until then, a pull fails with `authentication required`, and pulling it needs a token with `read:packages`: `gh auth refresh -s read:packages`, then `gh auth token | docker login ghcr.io -u <github user> --password-stdin`.

Each published tag is a multi-arch index with `linux/amd64` and `linux/arm64` images. The registry also lists two `unknown/unknown` entries: those are build provenance attestations, not images.

## Removing a service

Delete its Dockerfile, or the whole directory. CI stops building it. Images already published stay in GHCR until someone deletes the package.
