# AZIM-TAV

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

1. python3 -m venv venv
2. source venv/bin/activate
3. pip install -r requirements.txt

4.1. python run.py --reload
4.2. cloudflared tunnel --url http://localhost:8000

> 4.1. ile 4.2. adımlarını farklı terminalde çalıştırın.

- Sonrasında cloudflared size bir URL verecektir. Bu URL'yi tarayıcınızda açarak uygulamaya erişebilirsiniz.

---

- yerel paylaşım için:

> ip addr show wlp0s20f3
> sudo ufw allow 8000/tcp
> python run.py --reload

- Sonrasında `http://<IP_ADDRESS>:8000` adresine giderek uygulamaya erişebilirsiniz. `<IP_ADDRESS>` kısmını kendi IP adresinizle değiştirin.
