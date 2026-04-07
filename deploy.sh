#!/usr/bin/env bash
set -euo pipefail

SERVER="fedor@10.8.1.0"
REMOTE_DIR="/opt/guitar-bot"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== 1. Git commit & push ==="
cd "$REPO_DIR"
git add -A
if git diff --cached --quiet; then
    echo "Нет изменений для коммита"
else
    MSG="${1:-update: $(date +%Y-%m-%d_%H:%M)}"
    git commit -m "$MSG"
fi
git push origin main

echo ""
echo "=== 2. Pull on server ==="
ssh "$SERVER" "cd $REMOTE_DIR && git pull origin main 2>/dev/null || true"

echo ""
echo "=== 3. Sync files ==="
scp config.py generator.py image.py bot.py rubrics.json tone_profiles.json \
    requirements.txt Dockerfile docker-compose.yml "$SERVER:$REMOTE_DIR/"

echo ""
echo "=== 4. Rebuild & restart ==="
ssh "$SERVER" "cd $REMOTE_DIR && sudo docker compose up -d --build"

echo ""
echo "=== 5. Check ==="
sleep 3
ssh "$SERVER" "cd $REMOTE_DIR && sudo docker compose logs --tail 5"

echo ""
echo "✅ Deploy done!"
