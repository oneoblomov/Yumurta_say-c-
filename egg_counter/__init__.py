"""
Yumurta Sayıcı - Endüstriyel Gerçek Zamanlı Yumurta Sayma Sistemi
================================================================
Modüler mimari ile production-ready yumurta sayma pipeline'ı.

Modüller:
    config         - Merkezi konfigürasyon
    detector       - YOLO tabanlı algılama
    tracker        - ByteTrack tabanlı takip
    counter        - Sanal çizgi ile sayım
    visualizer     - Görsel işaretleme
    logger         - CSV / günlük log
    preprocessor   - Adaptif ön işleme
    pipeline       - Ana orkestratör
"""

__version__ = "1.0.0"
__author__ = "Azim-Tav"
