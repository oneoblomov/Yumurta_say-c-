# Yumurta Sayıcı - Systemd Kurulum Rehberi

Bu rehber, Yumurta Sayıcı uygulamasını Raspberry Pi 5 üzerinde systemd servisleri ile otomatik çalıştırmak için gereken adımları özetler.

## Hızlı Kurulum

Yeni bir cihazda önerilen akış:

```bash
git clone https://github.com/oneoblomov/Yumurta_say-c-.git
cd Yumurta_say-c-
sudo ./setup.sh
```

Bu komut en güncel GitHub Release paketini indirir, kurar ve servisleri başlatır.

## Gereksinimler

- Raspberry Pi 5
- Python 3.8+
- Git
- Cloudflared
- Systemd

## Otomatik Kurulumda Yapılanlar

- Sistem paketleri güncellenir.
- Gerekli paketler yüklenir.
- Son GitHub Release paketi cihaza açılır.
- Python bağımlılıkları kurulur.
- Cloudflared kurulumu doğrulanır.
- Systemd servis ve timer dosyaları kopyalanır.
- Servisler etkinleştirilir ve başlatılır.
- Güncelleme ve sağlık logları hazırlanır.

## Manuel Kurulum

1. Depoyu klonlayın.

```bash
git clone https://github.com/oneoblomov/Yumurta_say-c-.git
cd Yumurta_say-c-
```

1. Bağımlılıkları yükleyin.

```bash
pip install -r requirements.txt
```

1. Cloudflared kurun.

```bash
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64
sudo mv cloudflared-linux-arm64 /usr/bin/cloudflared
sudo chmod +x /usr/bin/cloudflared
```

1. Systemd birimlerini kopyalayın.

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo cp systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

1. Servisleri etkinleştirin.

```bash
sudo systemctl enable runpy.service
sudo systemctl enable cloudflared.service
sudo systemctl enable egg-counter-start.timer
sudo systemctl enable egg-counter-stop.timer
sudo systemctl enable update.timer
sudo systemctl enable health-check.timer
sudo systemctl enable cam-watchdog.timer
```

1. Servisleri başlatın.

```bash
sudo systemctl start runpy.service
sudo systemctl start cloudflared.service
sudo systemctl start egg-counter-start.timer
sudo systemctl start egg-counter-stop.timer
sudo systemctl start update.timer
sudo systemctl start health-check.timer
sudo systemctl start cam-watchdog.timer
```

## Servisler

- `runpy.service`: Web arayüzünü çalıştırır.
- `cloudflared.service`: Cloudflare tunnel bağlantısını açar.
- `egg-counter.service`: Ana sayım sürecini çalıştırır.
- `egg-counter-start.timer`: Sayım servisini günlük başlatır.
- `egg-counter-stop.timer`: Sayım servisini günlük durdurur.
- `update.service` ve `update.timer`: GitHub Release tabanlı güncelleme kontrolünü yapar.
- `health-check.service` ve `health-check.timer`: Servis sağlığını düzenli kontrol eder.
- `cam-watchdog.service` ve `cam-watchdog.timer`: Web ayarlarındaki kamera saatlerine göre pipeline'ı dışarıdan failsafe olarak kontrol eder.

## Güncelleme Akışı

- Web arayüzü yeni sürüm kontrolünü API üzerinden yapar.
- Arka planda `update_and_restart.sh`, `manage_update.py` üzerinden kurulum veya rollback başlatır.
- `update.timer` ayara göre yalnızca kontrol eder veya otomatik kurulum başlatır.
- Release geçmişi GitHub release listesi ve yerel kurulum kayıtları üzerinden izlenir.

## İzleme ve Sorun Giderme

Servis durumu:

```bash
sudo systemctl status runpy.service
```

Canlı log:

```bash
sudo journalctl -u runpy.service -f
```

Timer listesi:

```bash
sudo systemctl list-timers
```

Updater durumu:

```bash
python3 manage_update.py status
```

Elle sürüm kontrolü:

```bash
python3 manage_update.py check
```

## Güvenlik Notları

- Servisler `pi` kullanıcısı ile çalışır.
- Hassas dosyalar repoda tutulmamalıdır.
- SSH erişimi kısıtlanmalıdır.
- Disk ve bellek kullanımı izlenmelidir.
