"""
tracker.py - Takip Yönetimi + Spatial Dedup Modülü
====================================================
Düzeltmeler:
  1. Spatial deduplication: ByteTrack yeni ID verince, aynı bölgede eski
     sayılmış ID varsa yeni ID'yi otomatik "sayılmış" işaretle -> çift sayım engellensin.
  2. _prev_cy yerine trail'den yön tespiti (N frame geriye bakarak) -> daha güvenilir.
  3. Memory leak düzeltmesi: _cleanup_stale_tracks artık last_seen/first_seen'ı da temizliyor.
  4. ID yeniden atama tespiti: Kayıp ID'nin son pozisyonunu hatırlayıp yeni yakın ID'yi eşle.
"""

from collections import defaultdict, deque
from typing import Dict, Set, Optional, Tuple
import math

from .config import TrackerConfig, CounterConfig


class TrackManager:
    """
    Track ID yaşam döngüsü + spatial deduplication.

    Yeni ID atandığında, yakın mesafede son kayıp/sayılmış bir ID varsa
    yeni ID de "sayılmış" olarak işaretlenir -> çift sayım önlenir.
    """

    def __init__(self, tracker_cfg: TrackerConfig, counter_cfg: CounterConfig,
                 trail_length: int = 20):
        self.tracker_cfg = tracker_cfg
        self.counter_cfg = counter_cfg
        self.trail_length = trail_length

        # geometry information filled by pipeline
        self._frame_height: Optional[int] = None
        self._line_y: Optional[int] = None

        # ID -> deque[(cx, cy)]
        self.trails: Dict[int, deque] = defaultdict(
            lambda: deque(maxlen=self.trail_length)
        )

        self.first_seen: Dict[int, int] = {}
        self.last_seen: Dict[int, int] = {}
        self.counted_ids: Set[int] = set()
        self.counted_at_frame: Dict[int, int] = {}
        self.track_ages: Dict[int, int] = defaultdict(int)

        self._frame_count: int = 0
        self._active_ids: Set[int] = set()

        # --- DÜZELTME: Kayıp ID'lerin son pozisyonları (spatial dedup için) ---
        self._lost_track_positions: Dict[int, Tuple[int, int]] = {}  # ID -> (cx, cy)
        self._lost_track_frame: Dict[int, int] = {}  # ID -> kaybolduğu frame

        # Önceki frame aktif ID'leri (kayıp tespiti için)
        self._prev_active_ids: Set[int] = set()

    def update(self, detections: list) -> list:
        """
        Frame algılamalarını işle + spatial deduplication uygula.

        Pipeline init aşamasında ``set_frame_height`` çağrılmış olmalı;
        ayrıca çizgi konumu değişirse ``set_line_y`` çağrısına ihtiyaç var.
        Bu bilgiler spatial dedup fonksiyonuna çizgiden geçmiş olup olmadığını
        söyleyebilmek için gereklidir. (Aksi halde ROI giriş/çıkışında yeni
        tracklar eski sayılmış ID'lerle eşleşiyor, hatalı "ROI sayıldı"
        durumuna yol açıyor.)
        """
        self._frame_count += 1
        current_active = set()
        drop_frames = self.counter_cfg.post_cross_drop_frames

        enriched = []
        for det in detections:
            tid = det.get("track_id")
            if tid is None:
                enriched.append({
                    **det, "track_age": 0, "is_counted": False, "direction": None
                })
                continue

            cx, cy = det["center"]
            current_active.add(tid)

            # --- post_cross_drop: sayıldıktan N frame sonra takibi bırak ---
            if drop_frames > 0 and tid in self.counted_at_frame:
                frames_since = self._frame_count - self.counted_at_frame[tid]
                if frames_since >= drop_frames:
                    # Trail'i temizle (bellek + görselleme kazanımı)
                    self.trails.pop(tid, None)
                    # Bu detection'u enriched'e ekleme -> pipeline işlemiyor
                    continue

            # Trail güncelle
            self.trails[tid].append((cx, cy))

            if tid not in self.first_seen:
                self.first_seen[tid] = self._frame_count

                # --- SPATIAL DEDUP: Yeni ID mı? Yakınında eski sayılmış ID var mı? ---
                dedup_result = self._check_spatial_dedup(tid, cx, cy)
                if dedup_result is not None:
                    # Bu yeni ID aslında eski sayılmış bir yumurta
                    self.counted_ids.add(tid)
                    self.counted_at_frame[tid] = self._frame_count

            self.last_seen[tid] = self._frame_count
            self.track_ages[tid] += 1

            # Yön tespiti: Trail'den son 3 noktaya bakarak (eski: sadece 1 önceki)
            direction = self._get_direction(tid)

            enriched.append({
                **det,
                "track_age": self.track_ages[tid],
                "is_counted": tid in self.counted_ids,
                "direction": direction,
            })

        # Kayıp olan ID'lerin son pozisyonlarını kaydet
        lost_ids = self._prev_active_ids - current_active
        for tid in lost_ids:
            trail = self.trails.get(tid)
            if trail and len(trail) > 0:
                self._lost_track_positions[tid] = trail[-1]
                self._lost_track_frame[tid] = self._frame_count

        self._prev_active_ids = current_active.copy()
        self._active_ids = current_active

        # Eski izleri temizle
        self._cleanup_stale_tracks()

        return enriched

    def _check_spatial_dedup(self, new_tid: int, cx: int, cy: int) -> Optional[int]:
        """
        Yeni ID'nin yakınında bir kayıp/sayılmış ID var mı kontrol et.

        ByteTrack bazen aynı yumurtaya yeni ID atar (occlusion, frame drop).
        Bu fonksiyon mesafe kontrolü ile çift sayımı engeller.

        DÜZELTME: Inference boyutu değiştiğinde (örn. 320px) çizgiyi henüz
        geçmemiş yumurtalar yanlışlıkla önceki sayılmış bir ID ile eşleşip
        "sayılmış" olarak işaretleniyordu. Bunun önüne geçmek için:

        1. Yeni tespit çizginin üstündeyse (henüz sayılmamış taraf), eski
           sayılmış ID de çizginin üstündeyse → kesinlikle eşleştirme yapma.
        2. Eski ID'nin son pozisyonu çizginin "sayılmış bölgesini" yeterli
           marjinle geçmiş olmalı (crossing_margin * 2).
        3. Çok genç olmayan tracklar (5+ frame görülen) spatial dedup'a
           girmez — gerçekten yeni bir yumurtadır.

        Returns:
            Eşleşen eski ID (varsa), None (yoksa)
        """
        radius = self.counter_cfg.spatial_dedup_radius
        max_lost_age = self.tracker_cfg.track_buffer  # Bu kadar frame içinde kaybolmuş olmalı
        # Spatial dedup için minimum çizgi marjini — eski track bu kadar
        # çizgiyi geçmiş olmalı (küçük inference'da FP eşleşmeyi önler)
        line_margin = max(self.counter_cfg.crossing_margin * 3, 20)

        best_match = None
        best_dist = float('inf')

        line_y = self._line_y

        # Çok uzun süredir görülen track -> gerçek yeni yumurta, dedup'a girmesin
        track_age = self.track_ages.get(new_tid, 0)
        if track_age > 5:
            return None

        for old_tid, (ox, oy) in self._lost_track_positions.items():
            # Çok eski kayıp ID'leri atla
            lost_frame = self._lost_track_frame.get(old_tid, 0)
            if self._frame_count - lost_frame > max_lost_age:
                continue

            # Sadece sayılmış ID'lerle karşılaştır
            if old_tid not in self.counted_ids:
                continue

            # çizgi bilgisi varsa katı yön kontrolü uygula
            if line_y is not None:
                if self.counter_cfg.direction == "top_to_bottom":
                    # Yeni yumurta henüz çizgiyi geçmemişse (üstteyse):
                    # Eski yumurtanın AÇIKÇA çizgiyi geçmiş olması lazım
                    # (line_margin kadar ötede). Aksi halde false positive.
                    if cy < line_y:
                        if oy < line_y + line_margin:
                            # Eski track çizgiyi yeterince geçmemişti → eşleştirme
                            continue
                    # Yeni yumurta da çizgiyi geçmişse → spatial dedup anlamsız
                    # (zaten sayılacak), geç
                    if cy >= line_y:
                        continue

                elif self.counter_cfg.direction == "bottom_to_top":
                    if cy > line_y:
                        if oy > line_y - line_margin:
                            continue
                    if cy <= line_y:
                        continue
                # 'both' yönünde herhangi bir taraf kabul edilir

            dist = math.hypot(cx - ox, cy - oy)
            if dist < radius and dist < best_dist:
                best_dist = dist
                best_match = old_tid

        if best_match is not None:
            print(f"[TRACKER] Spatial dedup: Yeni #{new_tid} ≈ Eski #{best_match} "
                  f"(mesafe={best_dist:.0f}px) -> otomatik COUNTED")
        return best_match

    def _get_direction(self, tid: int) -> Optional[str]:
        """
        Trail'den yön tespit et. Son 3 noktanın ortalamasına bakar
        (eski: sadece son 1 nokta -> titreşimde hatalı).
        """
        trail = self.trails.get(tid)
        if trail is None or len(trail) < 2:
            return None

        if len(trail) >= 3:
            # Son 3 noktanın Y trendi
            y_values = [p[1] for p in list(trail)[-3:]]
            dy = y_values[-1] - y_values[0]
        else:
            dy = trail[-1][1] - trail[-2][1]

        if dy > 1:
            return "down"
        elif dy < -1:
            return "up"
        return "stationary"

    def mark_counted(self, track_id: int):
        self.counted_ids.add(track_id)
        self.counted_at_frame[track_id] = self._frame_count

    def can_be_counted(self, track_id: int) -> bool:
        """
        Sayılabilir mi?
        - Daha önce sayılmamış
        - Minimum yaş geçmiş
        - Cooldown geçmiş
        """
        if track_id in self.counted_ids:
            return False

        if self.track_ages.get(track_id, 0) < self.counter_cfg.min_track_age:
            return False

        if track_id in self.counted_at_frame:
            frames_since = self._frame_count - self.counted_at_frame[track_id]
            if frames_since < self.counter_cfg.double_count_cooldown:
                return False

        return True

    def get_trail(self, track_id: int) -> list:
        return list(self.trails.get(track_id, []))

    def get_active_count(self) -> int:
        return len(self._active_ids)

    def get_total_counted(self) -> int:
        return len(self.counted_ids)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def set_frame_height(self, height: int):
        """Pipeline tarafından çağrılır; kare yüksekliğini bildirir.

        ``_line_y`` çağrıldığında da güncellenir.
        """
        self._frame_height = height
        self._line_y = int(height * self.counter_cfg.line_position)

    def set_line_y(self, line_y: int):
        """Çizgi pozisyonu değiştiğinde pipeline bu metodu çağırır."""
        self._line_y = line_y

    def _cleanup_stale_tracks(self):
        """
        Bellek temizliği. Düzeltme: last_seen/first_seen da temizleniyor (eski: memory leak).
        """
        stale_threshold = self.tracker_cfg.track_buffer * 3
        stale_ids = [
            tid for tid, last in self.last_seen.items()
            if (self._frame_count - last) > stale_threshold
            and tid not in self._active_ids
        ]
        for tid in stale_ids:
            self.trails.pop(tid, None)
            self.track_ages.pop(tid, None)
            self.first_seen.pop(tid, None)
            self.last_seen.pop(tid, None)
            self._lost_track_positions.pop(tid, None)
            self._lost_track_frame.pop(tid, None)
            # counted_ids ve counted_at_frame TEMİZLENMEZ (log bütünlüğü)

    def reset(self):
        self.trails.clear()
        self.first_seen.clear()
        self.last_seen.clear()
        self.counted_ids.clear()
        self.counted_at_frame.clear()
        self.track_ages.clear()
        self._frame_count = 0
        self._active_ids.clear()
        self._prev_active_ids.clear()
        self._lost_track_positions.clear()
        self._lost_track_frame.clear()
