#!/bin/bash
cd /opt/memory-hub
OLD=$(git rev-parse HEAD)
git pull origin main --quiet 2>/dev/null
NEW=$(git rev-parse HEAD)
if [ "$OLD" != "$NEW" ]; then
    /opt/memory-hub/.venv/bin/pip install -r requirements.txt --quiet 2>/dev/null
    systemctl restart memory-hub
    echo "$(date): Deployed $NEW" >> /opt/memory-hub/deploy.log
fi
