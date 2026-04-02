#!/bin/bash
# scripts/backup.sh - Backup script for container volumes

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="${BACKUP_DIR:-/opt/backups}"
DATE=$(date +%Y%m%d_%H%M%S)

cd "$DEPLOY_DIR"

echo "=== Container Backup Script ==="
echo "Backup directory: $BACKUP_DIR"
echo "Timestamp: $DATE"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup named volumes
VOLUMES=("email-data" "toyfix-data" "shared-logs")

for vol in "${VOLUMES[@]}"; do
    echo ""
    echo "Backing up volume: $vol"
    
    # Check if volume exists
    if docker volume inspect "$vol" > /dev/null 2>&1; then
        docker run --rm \
            -v "${vol}:/data" \
            -v "${BACKUP_DIR}:/backup" \
            alpine \
            tar czf "/backup/${vol}_${DATE}.tar.gz" -C /data .
        
        echo "  -> ${vol}_${DATE}.tar.gz"
    else
        echo "  -> Volume not found, skipping"
    fi
done

# Backup configurations
echo ""
echo "Backing up configurations..."
tar czf "${BACKUP_DIR}/configs_${DATE}.tar.gz" \
    docker-compose.yml \
    nginx/conf.d/ \
    services/*/docker-compose.yml \
    2>/dev/null || true

echo "  -> configs_${DATE}.tar.gz"

# Cleanup old backups (keep last 7 days)
echo ""
echo "Cleaning up old backups (keeping last 7 days)..."
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +7 -delete 2>/dev/null || true

echo ""
echo "=== Backup Complete ==="
echo ""
echo "Available backups:"
ls -lh "$BACKUP_DIR"/*.tar.gz 2>/dev/null || echo "No backups found"
