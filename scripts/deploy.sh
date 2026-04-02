#!/bin/bash
# scripts/deploy.sh - Deployment script for container-deploy

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"

cd "$DEPLOY_DIR"

echo "=== Container Deployment Script ==="
echo "Deploy directory: $DEPLOY_DIR"

# Load environment variables
if [ -f .env ]; then
    echo "Loading .env file..."
    set -a
    source .env
    set +a
else
    echo "Warning: .env file not found. Some services may not start properly."
fi

# Parse command line arguments
ACTION="${1:-up}"

case "$ACTION" in
    up|start)
        echo "Starting all services..."
        docker-compose up -d --build
        ;;
    down|stop)
        echo "Stopping all services..."
        docker-compose down
        ;;
    restart)
        echo "Restarting all services..."
        docker-compose restart
        ;;
    rebuild)
        echo "Rebuilding all services..."
        docker-compose build --pull
        docker-compose up -d --force-recreate
        ;;
    logs)
        SERVICE="${2:-}"
        if [ -n "$SERVICE" ]; then
            echo "Showing logs for: $SERVICE"
            docker-compose logs -f "$SERVICE"
        else
            echo "Showing logs for all services"
            docker-compose logs -f
        fi
        ;;
    status)
        echo "=== Service Status ==="
        docker-compose ps
        ;;
    clean)
        echo "Cleaning up unused containers, networks, and images..."
        docker-compose down --rmi local --volumes
        ;;
    *)
        echo "Usage: $0 {up|down|restart|rebuild|logs|status|clean}"
        echo ""
        echo "Commands:"
        echo "  up      - Start all services (default)"
        echo "  down    - Stop all services"
        echo "  restart - Restart all services"
        echo "  rebuild - Rebuild and recreate all services"
        echo "  logs    - Show logs (optional: service name)"
        echo "  status  - Show service status"
        echo "  clean   - Remove containers, images, and volumes"
        exit 1
        ;;
esac

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Services available at:"
echo "  - Email Parser: http://email.local (or email.{domain})"
echo "  - DingTalk Plugin: http://dingtalk.local (or dingtalk.{domain})"
echo "  - ToyFix System: http://toyfix.local (or toyfix.{domain})"
echo ""
echo "Add entries to /etc/hosts for local testing:"
echo "  127.0.0.1 email.local dingtalk.local toyfix.local"
