#!/bin/bash
# health_check.sh - Servislerin durumunu kontrol et ve gerekirse tamir et

LOG_FILE="/home/kaplan/Desktop/Azim-Tav/Yumurta_sayıcı/health.log"

echo "$(date): Sağlık kontrolü başlatıldı." >> "$LOG_FILE"

# runpy.service kontrolü
if ! systemctl is-active --quiet runpy.service; then
    echo "$(date): runpy.service aktif değil, yeniden başlatılıyor." >> "$LOG_FILE"
    sudo systemctl restart runpy.service 2>>"$LOG_FILE"
    sleep 5
    if systemctl is-active --quiet runpy.service; then
        echo "$(date): runpy.service başarıyla yeniden başlatıldı." >> "$LOG_FILE"
    else
        echo "$(date): runpy.service yeniden başlatma başarısız." >> "$LOG_FILE"
    fi
else
    echo "$(date): runpy.service aktif." >> "$LOG_FILE"
fi

# cloudflared.service kontrolü
if ! systemctl is-active --quiet cloudflared.service; then
    echo "$(date): cloudflared.service aktif değil, yeniden başlatılıyor." >> "$LOG_FILE"
    sudo systemctl restart cloudflared.service 2>>"$LOG_FILE"
    sleep 5
    if systemctl is-active --quiet cloudflared.service; then
        echo "$(date): cloudflared.service başarıyla yeniden başlatıldı." >> "$LOG_FILE"
    else
        echo "$(date): cloudflared.service yeniden başlatma başarısız." >> "$LOG_FILE"
    fi
else
    echo "$(date): cloudflared.service aktif." >> "$LOG_FILE"
fi

# egg-counter.service kontrolü (sadece çalışma saatlerinde)
HOUR=$(date +%H)
if [ "$HOUR" -ge 8 ] && [ "$HOUR" -lt 18 ]; then
    if ! systemctl is-active --quiet egg-counter.service; then
        echo "$(date): egg-counter.service aktif değil (çalışma saati), yeniden başlatılıyor." >> "$LOG_FILE"
        sudo systemctl restart egg-counter.service 2>>"$LOG_FILE"
        sleep 5
        if systemctl is-active --quiet egg-counter.service; then
            echo "$(date): egg-counter.service başarıyla yeniden başlatıldı." >> "$LOG_FILE"
        else
            echo "$(date): egg-counter.service yeniden başlatma başarısız." >> "$LOG_FILE"
        fi
    else
        echo "$(date): egg-counter.service aktif." >> "$LOG_FILE"
    fi
else
    echo "$(date): egg-counter.service çalışma saati dışında, kontrol edilmiyor." >> "$LOG_FILE"
fi

# Disk alanı kontrolü (kritik seviye)
DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
if [ "$DISK_USAGE" -gt 90 ]; then
    echo "$(date): Disk kullanımı %$DISK_USAGE, kritik seviye! Log temizliği önerilir." >> "$LOG_FILE"
    # Otomatik temizlik: eski logları sil
    sudo journalctl --vacuum-time=7d 2>>"$LOG_FILE"
    echo "$(date): Eski loglar temizlendi." >> "$LOG_FILE"
fi

# Bellek kontrolü
MEM_USAGE=$(free | grep Mem | awk '{printf "%.0f", $3/$2 * 100.0}')
if [ "$MEM_USAGE" -gt 85 ]; then
    echo "$(date): Bellek kullanımı %$MEM_USAGE, yüksek seviye." >> "$LOG_FILE"
    # Servisleri yeniden başlatmayı düşün, ama dikkatli ol
fi

echo "$(date): Sağlık kontrolü tamamlandı." >> "$LOG_FILE"