# Contributing

Thanks for helping improve Share Sentinel.

This project is split across an API, a worker, a UI, and a collector. The quickest way to stay productive is to keep changes small, verify the affected surface, and update docs whenever behavior changes.

## Prerequisites

- Docker and Docker Compose
- Python 3.11 if you want to run services or tests outside containers
- A copy of `.env` based on `.env.example`
- Placeholder values in `.env` replaced before you start the stack

## Local development

1. Copy the example environment:

```bash
cp .env.example .env
```

2. Build and start the local stack:

```bash
docker compose up --build
```

3. Open the app at `http://localhost`.

4. Optional route smoke test:

```bash
./scripts/smoke-routes.sh http://localhost
```

The Compose stack runs a `bootstrap` container that applies Alembic migrations and seeds the initial admin account before the API and worker start.

## Repo layout

- `api/` FastAPI service and Alembic migrations
- `worker/` background ingest worker
- `ui/` React + Vite frontend
- `collector/` Python collection CLI
- `docs/` product and reference docs

## Testing

Run the narrowest test set that proves your change, then widen out before merging.

Examples:

```bash
cd api && pip install -r requirements-dev.txt && pytest -q
cd worker && pip install -r requirements-dev.txt && pytest -q
cd collector && pip install -r requirements-dev.txt && pytest -q
cd ui && npm ci && npm run typecheck && npm run build
```

If you prefer to stay inside the running stack:

```bash
docker compose exec -T api bash -lc "pip install -q -r requirements-dev.txt && pytest -q"
```

For behavior that crosses services, rebuild the affected containers and rerun:

```bash
docker compose up -d --build
./scripts/smoke-routes.sh http://localhost
```

## Migrations and schema changes

If you change the API data model:

1. add an Alembic migration under `api/alembic/versions/`
2. rebuild or rerun `bootstrap`
3. verify the stack comes back cleanly

## Documentation expectations

Docs are part of the product surface. Update them when you change:

- routes or response behavior
- authentication or deployment assumptions
- settings and admin workflows
- collector or artifact format behavior
- run explorer or inventory UX

The usual starting points are:

- `README.md`
- `docs/reference/api.md`
- `docs/reference/auth-rbac.md`
- `docs/reference/frontend.md`
- `docs/pages/settings.md`
- subsystem READMEs in `api/`, `worker/`, and `collector/`

## Security and data-handling rules

- Use synthetic data only in tests, screenshots, sample artifacts, and issue attachments.
- Do not commit real `.env` values, browser cookies, API tokens, or captured customer artifacts.
- If your change touches auth, tokens, upload handling, proxy trust, RBAC, or security-sensitive defaults, call that out explicitly in the PR description.
- Public vulnerability reports should be redirected to [SECURITY.md](./SECURITY.md).

## Pull request guidance

- Prefer focused changes over large mixed refactors.
- Include tests for behavior changes when practical.
- Call out migrations, new env vars, and operational caveats in the PR description.
- If a known limitation remains, document it rather than implying it is solved.

## License

This repository is Apache-2.0 licensed. Unless the maintainers publish a separate CLA or DCO process, assume inbound contributions are accepted under the repository license terms.

Accuracy beats breadth. Honest small improvements are better than sweeping but partially verified changes.
