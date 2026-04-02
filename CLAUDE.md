# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Container orchestration deployment for Leeteam multi-service architecture. Three services behind an nginx reverse proxy:
- **email-parser** (FastAPI, port 8000) — Email indexing and search
- **dingtalk-plugin** (React/CRA frontend + Express auth server, ports 3000/3001)
- **toyfix-system** (Vue 3 frontend + FastAPI backend, ports 5173/8001)

## Common Commands

```bash
# Production deployment
./scripts/deploy.sh up          # Build and start all services
./scripts/deploy.sh down        # Stop all services
./scripts/deploy.sh restart     # Restart services
./scripts/deploy.sh rebuild     # Rebuild all images and recreate containers
./scripts/deploy.sh status      # Show container status
./scripts/deploy.sh logs [svc]  # Tail logs (all or specific service)
./scripts/deploy.sh clean       # Remove containers, images, and volumes

# Backup
./scripts/backup.sh

# Development (with hot-reload via volume mounts)
docker-compose -f docker-compose.yml -f docker-compose.override.yml up
```

## Architecture

**nginx reverse proxy** — Routes incoming requests by subdomain (`email.*`, `dingtalk.*`, `toyfix.*`) to the appropriate backend service. The `nginx.conf` defines the main config, with per-service routing in `nginx/conf.d/*.conf`. nginx `depends_on` all backends with `condition: service_healthy`, so it won't start until all backends are healthy.

**Service health checks** — All containers have health checks (FastAPI services use `/health` endpoint, Node services use wget spider). nginx waits for these before starting.

**Shared volumes** — `shared-logs` mounts to `/var/log/app` in all containers. Certificates live in `shared/certs/`.

**Data persistence** — `email-parser` data in `shared/email-parser-data/`, `toyfix-backend` data in a named volume.

## Development vs Production

- **Production**: `docker-compose up -d --build` — single command via deploy.sh
- **Development**: Uses `docker-compose.override.yml` which mounts source directories as volumes for hot-reloading and exposes ports directly (bypasses nginx)

## Local Testing

Add to `/etc/hosts`:
```
127.0.0.1 email.local dingtalk.local toyfix.local
```

## Service-Specific Docs

Each service directory may contain its own `AGENTS.md` with service-specific development details (e.g., `services/email-parser/AGENTS.md`).

## Adding a New Service

1. Create service directory under `services/`
2. Add `Dockerfile` to the service
3. Add nginx routing config in `nginx/conf.d/`
4. Add service entry to `docker-compose.yml`
5. Update nginx `depends_on` section in `docker-compose.yml`
