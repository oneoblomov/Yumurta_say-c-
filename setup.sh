#!/bin/bash
# setup.sh - GitHub Release paketi ile kurulum scripti

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_OWNER="oneoblomov"
REPO_NAME="Yumurta_say-c-"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$SCRIPT_DIR"

if [ ! -f "$TARGET_DIR/main.py" ]; then
    TARGET_DIR="$HOME/Yumurta_sayıcı"
fi

mkdir -p "$TARGET_DIR"
LOG_FILE="$TARGET_DIR/setup.log"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  YUMURTA SAYICI - İLK KURULUM${NC}"
echo -e "${GREEN}  GitHub Release Paketi Kurulumu${NC}"
echo -e "${GREEN}========================================${NC}"
echo "$(date): Kurulum başlatıldı." > "$LOG_FILE"

echo -e "${YELLOW}1. Sistem paketleri hazırlanıyor...${NC}"
sudo apt update >> "$LOG_FILE" 2>&1
sudo apt install -y python3 python3-pip python3-venv git curl wget tar rsync >> "$LOG_FILE" 2>&1
echo -e "${GREEN}✓ Sistem hazır.${NC}"

echo -e "${YELLOW}2. Son release paketi alınıyor...${NC}"
RELEASE_JSON=$(python3 - "$REPO_OWNER" "$REPO_NAME" <<'PY'
import json
import sys
import urllib.request

owner, repo = sys.argv[1], sys.argv[2]
url = f"https://api.github.com/repos/{owner}/{repo}/releases"
req = urllib.request.Request(url, headers={"User-Agent": "yumurta-sayici-setup", "Accept": "application/vnd.github+json"})
with urllib.request.urlopen(req, timeout=20) as resp:
    releases = json.load(resp)

for release in releases:
    if release.get("draft") or release.get("prerelease"):
        continue
    package_asset = None
    checksum_asset = None
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.startswith("yumurta-sayici-") and name.endswith(".tar.gz"):
            package_asset = asset
        if name.startswith("yumurta-sayici-") and name.endswith(".sha256"):
            checksum_asset = asset
    if package_asset:
        print(json.dumps({
            "version": (release.get("tag_name") or "").lstrip("v"),
            "package_url": package_asset.get("browser_download_url"),
            "package_name": package_asset.get("name"),
            "checksum_url": checksum_asset.get("browser_download_url") if checksum_asset else "",
        }))
        break
else:
    raise SystemExit("Release paketi bulunamadı")
PY
) || true

USE_LOCAL_SOURCE=0
if [ -z "$RELEASE_JSON" ]; then
    if [ -f "$SCRIPT_DIR/main.py" ]; then
        USE_LOCAL_SOURCE=1
        echo "$(date): GitHub release paketi alınamadı, yerel dosyalar kullanılacak." >> "$LOG_FILE"
    else
        echo -e "${RED}Release paketi alınamadı.${NC}"
        exit 1
    fi
fi

LATEST_VERSION=""
PACKAGE_NAME=""
LOCAL_PACKAGE_PATH=""

if [ "$USE_LOCAL_SOURCE" -eq 0 ]; then
    LATEST_VERSION=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["version"])' "$RELEASE_JSON")
    PACKAGE_URL=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["package_url"])' "$RELEASE_JSON")
    PACKAGE_NAME=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["package_name"])' "$RELEASE_JSON")
    CHECKSUM_URL=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("checksum_url", ""))' "$RELEASE_JSON")
    TMP_DIR=$(mktemp -d)
    PACKAGE_PATH="$TMP_DIR/$PACKAGE_NAME"

    python3 - "$PACKAGE_URL" "$PACKAGE_PATH" <<'PY'
import shutil
import sys
import urllib.request

url, destination = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={"User-Agent": "yumurta-sayici-setup"})
with urllib.request.urlopen(req, timeout=60) as resp, open(destination, "wb") as handle:
    shutil.copyfileobj(resp, handle)
PY

    if [ -n "$CHECKSUM_URL" ]; then
        python3 - "$CHECKSUM_URL" "$PACKAGE_PATH" <<'PY'
import hashlib
import sys
import urllib.request

checksum_url, package_path = sys.argv[1], sys.argv[2]
req = urllib.request.Request(checksum_url, headers={"User-Agent": "yumurta-sayici-setup"})
with urllib.request.urlopen(req, timeout=20) as resp:
    payload = resp.read().decode("utf-8")

expected = None
for line in payload.splitlines():
    parts = line.split()
    if not parts:
        continue
    if package_path.split("/")[-1] in line:
        expected = parts[0]
        break
    if expected is None:
        expected = parts[0]

digest = hashlib.sha256(open(package_path, "rb").read()).hexdigest()
if expected and digest != expected:
    raise SystemExit("Checksum doğrulaması başarısız")
PY
    fi

    EXTRACT_DIR="$TMP_DIR/extracted"
    mkdir -p "$EXTRACT_DIR"
    tar -xzf "$PACKAGE_PATH" -C "$EXTRACT_DIR"
    if [ -d "$EXTRACT_DIR/yumurta-sayici" ]; then
        PACKAGE_ROOT="$EXTRACT_DIR/yumurta-sayici"
    else
        PACKAGE_ROOT="$EXTRACT_DIR"
    fi

    mkdir -p "$TARGET_DIR/releases/packages"
    LOCAL_PACKAGE_PATH="$TARGET_DIR/releases/packages/$PACKAGE_NAME"
    cp "$PACKAGE_PATH" "$LOCAL_PACKAGE_PATH"

    rsync -a "$PACKAGE_ROOT/" "$TARGET_DIR/" \
        --exclude data \
        --exclude logs \
        --exclude releases \
        --exclude .git \
        --exclude .venv \
        --exclude venv >> "$LOG_FILE" 2>&1

    rm -rf "$TMP_DIR"
    echo -e "${GREEN}✓ Son paket yüklendi: v$LATEST_VERSION${NC}"
else
    if [ -f "$TARGET_DIR/VERSION" ]; then
        LATEST_VERSION=$(tr -d '[:space:]' < "$TARGET_DIR/VERSION")
    else
        LATEST_VERSION="1.0.0"
    fi
    echo -e "${YELLOW}Yerel kaynak kullanılacak: v$LATEST_VERSION${NC}"
fi

WORK_DIR="$TARGET_DIR"
LOG_FILE="$WORK_DIR/setup.log"

echo -e "${YELLOW}3. Python bağımlılıkları yükleniyor...${NC}"
cd "$WORK_DIR"
python3 -m pip install --upgrade pip >> "$LOG_FILE" 2>&1
python3 -m pip install -r requirements.txt >> "$LOG_FILE" 2>&1
if [ -f requirements_web.txt ]; then
    python3 -m pip install -r requirements_web.txt >> "$LOG_FILE" 2>&1
fi
echo -e "${GREEN}✓ Python bağımlılıkları yüklendi.${NC}"

echo -e "${YELLOW}4. Cloudflared kontrol ediliyor...${NC}"
if ! command -v cloudflared >/dev/null 2>&1; then
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -O /tmp/cloudflared
    sudo mv /tmp/cloudflared /usr/bin/cloudflared
    sudo chmod +x /usr/bin/cloudflared
    echo -e "${GREEN}✓ Cloudflared yüklendi.${NC}"
else
    echo -e "${GREEN}✓ Cloudflared zaten yüklü.${NC}"
fi

echo -e "${YELLOW}5. Mevcut sürüm kaydediliyor...${NC}"
if [ -f "$WORK_DIR/manage_update.py" ]; then
    python3 "$WORK_DIR/manage_update.py" register-current --version "$LATEST_VERSION" --package-path "$LOCAL_PACKAGE_PATH" >> "$LOG_FILE" 2>&1 || true
fi
echo -e "${GREEN}✓ Sürüm kaydı tamamlandı.${NC}"

echo -e "${YELLOW}6. Systemd dosyaları kuruluyor...${NC}"
sudo cp systemd/*.service /etc/systemd/system/ >> "$LOG_FILE" 2>&1
sudo cp systemd/*.timer /etc/systemd/system/ >> "$LOG_FILE" 2>&1
sudo systemctl daemon-reload >> "$LOG_FILE" 2>&1
echo -e "${GREEN}✓ Systemd servisleri kuruldu.${NC}"

echo -e "${YELLOW}7. Servisler etkinleştiriliyor...${NC}"
sudo systemctl enable runpy.service >> "$LOG_FILE" 2>&1
sudo systemctl enable cloudflared.service >> "$LOG_FILE" 2>&1
sudo systemctl enable egg-counter-start.timer >> "$LOG_FILE" 2>&1
sudo systemctl enable egg-counter-stop.timer >> "$LOG_FILE" 2>&1
sudo systemctl enable update.timer >> "$LOG_FILE" 2>&1
sudo systemctl enable health-check.timer >> "$LOG_FILE" 2>&1
echo -e "${GREEN}✓ Servisler etkinleştirildi.${NC}"

echo -e "${YELLOW}8. Servisler başlatılıyor...${NC}"
sudo systemctl start runpy.service >> "$LOG_FILE" 2>&1
sudo systemctl start cloudflared.service >> "$LOG_FILE" 2>&1
sudo systemctl start egg-counter-start.timer >> "$LOG_FILE" 2>&1
sudo systemctl start egg-counter-stop.timer >> "$LOG_FILE" 2>&1
sudo systemctl start update.timer >> "$LOG_FILE" 2>&1
sudo systemctl start health-check.timer >> "$LOG_FILE" 2>&1
echo -e "${GREEN}✓ Servisler başlatıldı.${NC}"

echo -e "${YELLOW}9. Log dosyaları hazırlanıyor...${NC}"
touch "$WORK_DIR/update.log"
touch "$WORK_DIR/health.log"
echo -e "${GREEN}✓ Log dosyaları hazır.${NC}"

echo -e "${YELLOW}10. Kurulum doğrulanıyor...${NC}"
sleep 5
if systemctl is-active --quiet runpy.service; then
    echo -e "${GREEN}✓ runpy.service aktif${NC}"
else
    echo -e "${RED}✗ runpy.service aktif değil${NC}"
fi

if systemctl is-active --quiet cloudflared.service; then
    echo -e "${GREEN}✓ cloudflared.service aktif${NC}"
else
    echo -e "${RED}✗ cloudflared.service aktif değil${NC}"
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  KURULUM TAMAMLANDI!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "Yuklenen surum: ${YELLOW}v$LATEST_VERSION${NC}"
echo -e "Web arayuzu: ${YELLOW}http://localhost:8000${NC}"
echo -e "Otomatik guncelleme: ${YELLOW}GitHub Release + update.timer${NC}"
echo -e ""
echo -e "Log dosyalari:"
echo -e "  Kurulum: ${YELLOW}$LOG_FILE${NC}"
echo -e "  Guncelleme: ${YELLOW}$WORK_DIR/update.log${NC}"
echo -e "  Saglik: ${YELLOW}$WORK_DIR/health.log${NC}"
echo -e ""
echo -e "Komutlar:"
echo -e "  Guncelleme durumu: ${YELLOW}python3 $WORK_DIR/manage_update.py status${NC}"
echo -e "  Elle kontrol: ${YELLOW}python3 $WORK_DIR/manage_update.py check${NC}"
echo -e "  Son surumu kur: ${YELLOW}python3 $WORK_DIR/manage_update.py install --restart${NC}"
echo -e ""
echo "$(date): Kurulum başarıyla tamamlandı." >> "$LOG_FILE"