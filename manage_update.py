#!/usr/bin/env python3
"""CLI entrypoint for release-based updates."""

from __future__ import annotations

import argparse
import json
import sys

from web.database import Database
from web.update_manager import UpdateError, UpdateManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Yumurta Sayıcı güncelleme yöneticisi")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Yeni sürüm kontrolü yap")
    check_parser.add_argument("--no-notify", action="store_true", help="Alert üretme")

    subparsers.add_parser("auto", help="Ayarları okuyup otomatik kontrol/kurulum yap")

    install_parser = subparsers.add_parser("install", help="Belirli veya son sürümü yükle")
    install_parser.add_argument("--version", help="Yüklenecek sürüm")
    install_parser.add_argument("--restart", action="store_true", help="Kurulumdan sonra servisleri yeniden başlat")

    rollback_parser = subparsers.add_parser("rollback", help="Eski sürüme dön")
    rollback_parser.add_argument("--version", required=True, help="Geri dönülecek sürüm")
    rollback_parser.add_argument("--restart", action="store_true", help="Kurulumdan sonra servisleri yeniden başlat")

    subparsers.add_parser("restart", help="Servisleri yeniden başlat")
    subparsers.add_parser("status", help="Kalıcı güncelleme durumunu yazdır")
    subparsers.add_parser("list-releases", help="GitHub sürümlerini yazdır")

    register_parser = subparsers.add_parser("register-current", help="Mevcut kurulum sürümünü kaydet")
    register_parser.add_argument("--version", help="Zorlanacak sürüm değeri")
    register_parser.add_argument("--package-path", help="İndirilen paket yolu")

    args = parser.parse_args()
    db = Database()
    manager = UpdateManager(db=db)

    try:
        if args.command == "check":
            result = manager.check_for_updates(notify=not args.no_notify)
        elif args.command == "auto":
            result = manager.auto_update()
        elif args.command == "install":
            result = manager.install_release(version=args.version, restart=args.restart, source="manual")
        elif args.command == "rollback":
            result = manager.rollback_to_version(version=args.version, restart=args.restart)
        elif args.command == "restart":
            manager.restart_services()
            result = manager.write_status(
                state="completed",
                message="Servisler yeniden başlatıldı",
                finished_at=None,
                error=None,
            )
        elif args.command == "status":
            result = manager.get_status()
        elif args.command == "list-releases":
            result = {"releases": manager.list_releases()}
        elif args.command == "register-current":
            result = manager.register_current_install(version=args.version, package_path=args.package_path)
        else:
            parser.error("Bilinmeyen komut")
            return 2
    except UpdateError as exc:
        result = manager.write_status(
            state="error",
            message="Güncelleme işlemi başarısız",
            error=str(exc),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    except Exception as exc:
        result = manager.write_status(
            state="error",
            message="Beklenmeyen güncelleme hatası",
            error=str(exc),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())