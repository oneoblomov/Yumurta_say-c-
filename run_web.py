#!/usr/bin/env python3
"""
run_web.py - Yumurta Sayıcı Web Arayüzü Başlatıcı
====================================================
Kullanım:
    python run_web.py                    # Varsayılan (0.0.0.0:8000)
    python run_web.py --host 127.0.0.1   # Sadece yerel
    python run_web.py --port 8080        # Özel port
    python run_web.py --reload           # Geliştirme modu
"""

import argparse
import sys
from pathlib import Path
import os
import socket
import threading
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from web.versioning import display_version, read_version


def main():
    parser = argparse.ArgumentParser(
        description="Yumurta Sayıcı Web Arayüzü")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Dinleme adresi (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000,
                        help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true",
                        help="Otomatik yeniden yükleme (geliştirme)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Worker sayısı (default: 1)")
    args = parser.parse_args()

    import uvicorn
    version_label = display_version(read_version())

    print(f"\n{'='*60}")
    print(f"  YUMURTA SAYICI WEB ARAYÜZÜ {version_label}")
    print(f"  Azim-Tav Endüstriyel Sayım Sistemi")
    print(f"{'='*60}")
    print(f"  Adres   : http://{args.host}:{args.port}")
    # try to show the cloudflared tunnel URL if the service has logged one
    try:
        from web.app import get_cloudflared_url
        cf = get_cloudflared_url()
    except Exception:
        cf = None
    if cf:
        print(f"  Cloudflare tunnel: {cf}")
    print(f"  Reload  : {'Açık' if args.reload else 'Kapalı'}")
    print(f"  Workers : {args.workers}")
    print(f"{'='*60}\n")

    # start watchdog keep‑alive thread if systemd is listening
    def _watchdog_pinger(interval: float = 30.0) -> None:
        """Periodically send WATCHDOG=1 to the systemd notify socket.
        Safe to call even when NOTIFY_SOCKET is unset; the loop will exit
        immediately in that case.
        """
        sockpath = os.environ.get("NOTIFY_SOCKET")
        if not sockpath:
            return
        # abstract namespace if leading @
        if sockpath[0] == "@":
            sockpath = "\0" + sockpath[1:]
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                s.connect(sockpath)
                s.sendall(b"WATCHDOG=1")
                s.close()
            except Exception:
                # ignore failures; we'll try again later
                pass
            time.sleep(interval)

    # launch pinger thread and detach so it lives with the process
    threading.Thread(target=_watchdog_pinger, daemon=True).start()

    uvicorn.run(
        "web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        log_level="info",
    )


if __name__ == "__main__":
    main()
