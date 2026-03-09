#!/usr/bin/env python3
"""
check_cam.py - Kamera kontrol scripti
======================================
systemd timer tarafından çağrılır, kamera açık değilse servisi yeniden başlatır.
"""

import cv2
import subprocess
import sys
from pathlib import Path

# Kamera kaynağını varsayalım (0), gerekirse config'den oku
CAMERA_SOURCE = 0

def main():
    cap = cv2.VideoCapture(CAMERA_SOURCE)
    if not cap.isOpened():
        print("Kamera açılamadı, servisi yeniden başlatıyorum")
        try:
            subprocess.run(["systemctl", "restart", "runpy.service"], check=True)
        except subprocess.CalledProcessError as e:
            print(f"Servis yeniden başlatma hatası: {e}")
            sys.exit(1)
    else:
        print("Kamera açık")
    cap.release()

if __name__ == "__main__":
    main()