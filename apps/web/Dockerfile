# Ember web app (EMBER-51). Build context is the repo root (docs/container-images.md):
#   docker build -f apps/web/Dockerfile -t ember/web:dev .
# SQLite lives in /data (mount a volume there). Set DATABASE_URL=postgres://... to use Postgres.

FROM node:24-slim AS deps
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-fund --no-audit

FROM node:24-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY apps/web/ ./
RUN npm run build

FROM node:24-slim AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    DATABASE_URL=file:/data/ember.db
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
# SQLite file location. Owned by nobody so a named volume mounted here is writable.
RUN mkdir /data && chown nobody:nogroup /data
USER nobody
EXPOSE 3000
CMD ["node", "server.js"]
