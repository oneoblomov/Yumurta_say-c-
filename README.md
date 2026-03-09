# AZIM-TAV

tek satırda kurulum

```shell
apt install -y curl #eğer yüklü degilse
curl -fsSL https://raw.githubusercontent.com/oneoblomov/Yumurta_say-c-/main/setup.sh | bash
```

## Guncelleme Sistemi

Bu proje artik GitHub Release paketleri ile guncellenir.

- Web arayuzundeki `Ayarlar > Guncelleme Sistemi` bolumunden yeni surum kontrolu yapilabilir.
- Yeni bir tag ornegin `v1.0.2` olarak yayinlandiginda GitHub Actions paket olusturur ve release asset yukler.
- Cihaz tarafinda `setup.sh` son release paketini kurar.
- Periyodik kontrol `update.timer` ile, uygulama ici manuel kontrol ise update API ile yapilir.
- Eski surumlere donus icin release listesinde ilgili surum secilerek rollback baslatilabilir.

Elle guncelleme komutlari:

```bash
python3 manage_update.py check
python3 manage_update.py install --restart
python3 manage_update.py rollback --version 1.0.1 --restart
python3 manage_update.py status
```

- cloudflared ile:

```bash
# GPG anahtarını ekle
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg

# Repo ekle
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflare-main $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflare-main.list

# Güncelle ve kur
sudo apt update
sudo apt install cloudflared
```

1. Sunucuyu hazirlayin:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

1. Uygulamayi ve tüneli ayri terminallerde calistirin:

```bash
python run_web.py --reload
cloudflared tunnel --url http://localhost:8000
```

> 4.1. ile 4.2. adımlarını farklı terminalde çalıştırın.

- Sonrasında cloudflared size bir URL verecektir. Bu URL'yi tarayıcınızda açarak uygulamaya erişebilirsiniz.

---

- yerel paylaşım için:

> ip addr show wlp0s20f3
> sudo ufw allow 8000/tcp
> python run_web.py --reload

- Sonrasında `http://<IP_ADDRESS>:8000` adresine giderek uygulamaya erişebilirsiniz. `<IP_ADDRESS>` kısmını kendi IP adresinizle değiştirin.

## Test ortamı

```shell
# Şu anda sorunlu düzenlenecek.(ek adımlar isteniyor)
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
docker run --rm --platform linux/arm64 -it debian:bookworm bash
```
