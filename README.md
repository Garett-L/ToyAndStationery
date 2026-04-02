# Container Orchestration Deployment

Multi-service container deployment architecture for Leeteam projects.

## Projects

| Service | Frontend | Backend | Ports |
|---------|----------|--------|-------|
| email-parser | - | FastAPI | 8000 |
| dingtalk-plugin | React 18 (CRA) | Express.js | 3000, 3001 |
| toyfix-system | Vue 3 | FastAPI (to be created) | 5173, 8001 |

## Quick Start

```bash
# 1. Navigate to deploy directory
cd container-deploy

# 2. Copy environment template
cp .env.example .env
# Edit .env with your configuration

# 3. Deploy all services
./scripts/deploy.sh up

# 4. Check status
./scripts/deploy.sh status
```

## Directory Structure

```
container-deploy/
├── docker-compose.yml          # Root orchestrator
├── docker-compose.override.yml  # Local dev overrides (optional)
├── .env                        # Environment variables
├── nginx/
│   ├── Dockerfile
│   ├── nginx.conf
│   └── conf.d/                 # Subdomain routing configs
├── services/
│   ├── email-parser/
│   ├── dingtalk-plugin/
│   │   ├── frontend/
│   │   └── auth-server/
│   └── toyfix-system/
│       ├── frontend/
│       └── backend/
├── shared/
│   ├── logs/
│   └── certs/
└── scripts/
    ├── deploy.sh
    └── backup.sh
```

## Access URLs

| Service | Local URL | Production URL |
|---------|-----------|----------------|
| Email Parser | http://email.local | http://email.{domain} |
| DingTalk Plugin | http://dingtalk.local | http://dingtalk.{domain} |
| ToyFix System | http://toyfix.local | http://toyfix.{domain} |

**Note**: Add entries to `/etc/hosts` for local testing:
```
127.0.0.1 email.local dingtalk.local toyfix.local
```

## Management Commands

```bash
# Start services
./scripts/deploy.sh up

# Stop services
./scripts/deploy.sh down

# Restart services
./scripts/deploy.sh restart

# Rebuild all services
./scripts/deploy.sh rebuild

# View logs
./scripts/deploy.sh logs [service_name]

# Check status
./scripts/deploy.sh status

# Backup volumes
./scripts/backup.sh

# Clean up everything
./scripts/deploy.sh clean
```

## Adding New Services

1. Create service directory under `services/`
2. Add `Dockerfile` to the service
3. Add nginx config in `nginx/conf.d/`
4. Add service entry to `docker-compose.yml`
5. Update nginx `depends_on` section

See [../doc/container-orchestration-architecture.md](../doc/container-orchestration-architecture.md) for full architecture documentation.

## Development vs Production

### Development
```bash
# Uses docker-compose.override.yml for hot-reloading
docker-compose -f docker-compose.yml -f docker-compose.override.yml up
```

### Production
```bash
# Production mode with nginx reverse proxy
docker-compose up -d --build
```

## Health Monitoring

All services include health checks. View health status:
```bash
docker-compose ps
docker inspect --format='{{json .State.Health}}' <container_name>
```
