import cv2
import numpy as np
import time
import argparse
from numba import jit

class CentroidTracker:
    def __init__(self, maxDisappeared=30):
        self.nextObjectID = 0
        self.objects = {}
        self.disappeared = {}
        self.maxDisappeared = maxDisappeared

    def register(self, centroid):
        self.objects[self.nextObjectID] = centroid
        self.disappeared[self.nextObjectID] = 0
        self.nextObjectID += 1

    def deregister(self, objectID):
        del self.objects[objectID]
        del self.disappeared[objectID]

    def update(self, rects):
        if len(rects) == 0:
            for objectID in list(self.disappeared.keys()):
                self.disappeared[objectID] += 1
                if self.disappeared[objectID] > self.maxDisappeared:
                    self.deregister(objectID)
            return self.objects

        inputCentroids = np.zeros((len(rects), 2), dtype="int")
        for (i, (startX, startY, endX, endY)) in enumerate(rects):
            cX = int((startX + endX) / 2.0)
            cY = int((startY + endY) / 2.0)
            inputCentroids[i] = (cX, cY)

        if len(self.objects) == 0:
            for i in range(0, len(inputCentroids)):
                self.register(inputCentroids[i])
        else:
            objectIDs = list(self.objects.keys())
            objectCentroids = list(self.objects.values())

            D = np.linalg.norm(np.array(objectCentroids)[:, np.newaxis] - inputCentroids, axis=2)
            rows = D.min(axis=1).argsort()
            cols = D.argmin(axis=1)[rows]

            usedRows = set()
            usedCols = set()

            for (row, col) in zip(rows, cols):
                if row in usedRows or col in usedCols:
                    continue
                objectID = objectIDs[row]
                self.objects[objectID] = inputCentroids[col]
                self.disappeared[objectID] = 0
                usedRows.add(row)
                usedCols.add(col)

            unusedRows = set(range(0, D.shape[0])).difference(usedRows)
            unusedCols = set(range(0, D.shape[1])).difference(usedCols)

            for row in unusedRows:
                self.disappeared[objectIDs[row]] += 1
                if self.disappeared[objectIDs[row]] > self.maxDisappeared:
                    self.deregister(objectIDs[row])

            for col in unusedCols:
                self.register(inputCentroids[col])

        return self.objects

def nothing(x):
    pass

@jit(nopython=True)
def filter_contours(areas, perimeters, widths, heights, solidities, circularities,
                    min_area, max_area, min_aspect, max_aspect, min_solidity, min_circularity):
    n = len(areas)
    keep = np.zeros(n, dtype=np.bool_)
    for i in range(n):
        if areas[i] < min_area or areas[i] > max_area:
            continue
        if heights[i] == 0:
            continue
        aspect = widths[i] / heights[i]
        if aspect < min_aspect or aspect > max_aspect:
            continue
        if solidities[i] < min_solidity:
            continue
        if circularities[i] < min_circularity:
            continue
        keep[i] = True
    return keep

def split_large_contour(contour, full_mask, offset_x, offset_y, dist_thresh_percent=50):
    """
    Büyük bir konturu watershed ile ayırır.
    full_mask: tüm bant bölgesinin binary maskesi
    offset_x, offset_y: konturun orijinal görüntüdeki konumu (burada kullanılmıyor, ama uyum için)
    dist_thresh_percent: mesafe haritası eşiği (yüzde)
    Dönüş: alt konturların listesi (orijinal koordinatlarda)
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))  # Kernel tanımla
    x, y, w, h = cv2.boundingRect(contour)
    # Kontur için özel bir maske oluştur (sadece bu konturun piksellerini içeren)
    roi_mask = np.zeros((h, w), dtype=np.uint8)
    shifted = contour - [x, y]
    cv2.drawContours(roi_mask, [shifted], -1, 255, -1)
    # full_mask'tan aynı bölgeyi al ve kontur maskesi ile kesişimini al
    roi_full = full_mask[y:y+h, x:x+w]
    roi_isolated = cv2.bitwise_and(roi_full, roi_mask)

    # Mesafe dönüşümü
    dist = cv2.distanceTransform(roi_isolated, cv2.DIST_L2, 5)
    if dist.max() == 0:
        return [contour]  # Ayırma mümkün değil

    # Mesafe eşiği ile marker'ları bul
    thresh_val = (dist_thresh_percent / 100.0) * dist.max()
    _, markers = cv2.threshold(dist, thresh_val, 255, cv2.THRESH_BINARY)
    markers = markers.astype(np.uint8)

    # Marker'ların bağlantılı bileşenlerini bul
    num_markers, markers_cc = cv2.connectedComponents(markers)
    if num_markers <= 1:
        return [contour]  # Tek bir marker var, ayırma yok

    # Watershed için marker matrisini hazırla
    markers_ws = np.zeros(roi_isolated.shape, dtype=np.int32)
    markers_ws[markers_cc > 0] = markers_cc[markers_cc > 0]

    # Sure background ekle (mesafe 0 olan yerler)
    sure_bg = cv2.dilate(roi_isolated, kernel, iterations=3)
    markers_ws[sure_bg == 0] = 0  # Background

    # Sure foreground zaten markers_ws'ta

    # Watershed uygula
    roi_3ch = cv2.cvtColor(roi_isolated, cv2.COLOR_GRAY2BGR)
    cv2.watershed(roi_3ch, markers_ws)

    # Her bir label için kontur çıkar
    sub_contours = []
    for label in range(1, num_markers + 1):
        label_mask = np.zeros(roi_isolated.shape, dtype=np.uint8)
        label_mask[markers_ws == label] = 255
        cnts_lab, _ = cv2.findContours(label_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts_lab:
            # En büyük konturu al (genelde bir tane olur)
            cnt_lab = max(cnts_lab, key=cv2.contourArea)
            # Orijinal koordinatlara geri taşı
            cnt_lab_shifted = cnt_lab + [x, y]
            sub_contours.append(cnt_lab_shifted)

    if not sub_contours:
        return [contour]
    return sub_contours

def main():
    # parse command line option for video path
    parser = argparse.ArgumentParser(description='Yumurta sayma script')
    parser.add_argument('-v', '--video', dest='video',
                        help='Path to video file',
                        default=0)
    args = parser.parse_args()
    source = args.video

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"Video açılamadı: {source}")
        return

    # read first frame to obtain actual dimensions (some formats don't report props)
    ret, frame = cap.read()
    if not ret:
        print(f"İlk kare okunamadı: {source}")
        return
    orig_height, orig_width = frame.shape[:2]
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    scale_factor = 1.0
    frame_width = int(orig_width * scale_factor)
    frame_height = int(orig_height * scale_factor)

    window_name = "Yumurta Sayma (RPi5)"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.namedWindow("Mask", cv2.WINDOW_NORMAL)
    cv2.namedWindow("Ayarlar", cv2.WINDOW_NORMAL)

    # trackbar definitions as (name, init, max)
    trackbars = [
        ("H Min", 0, 180),
        ("H Max", 180, 180),
        ("S Min", 0, 255),
        ("S Max", 50, 255),
        ("V Min", 200, 255),
        ("V Max", 255, 255),
        ("Min Area", 700, 5000),
        ("Max Area", 5000, 20000),
        ("Min Aspect", 60, 100),
        ("Max Aspect", 160, 200),
        ("Min Solidity", 85, 100),
        ("Min Circularity", 60, 100),
        ("Max Disappeared", 30, 100),
        ("Size Scale (x100)", 50, 200),
        ("Scale Factor (x100)", int(scale_factor * 100), 200),
        ("Split Enable", 0, 1),
        ("Split Area Mult (x100)", 150, 300),
        ("Split Dist Thresh", 50, 100),
    ]

    for name, init, maxi in trackbars:
        cv2.createTrackbar(name, "Ayarlar", init, maxi, nothing)
    # set initial positions explicitly (redundant but keeps previous defaults clear)
    for name, init, _ in trackbars:
        cv2.setTrackbarPos(name, "Ayarlar", init)

    def get_lines(h):
        return int(h * 0.55), int(h * 0.7), int(h * 0.8)

    line1_y, line2_y, line3_y = get_lines(frame_height)

    count = 0
    counted_ids = set()
    prev_centroids = {}
    heatmap = np.zeros((frame_height, frame_width), dtype=np.float32)

    tracker = CentroidTracker(maxDisappeared=30)

    prev_time = 0
    paused = False

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    eggs_green_mask = None
    heatmap_update_counter = 0
    HEATMAP_UPDATE_FREQ = 10

    while True:
        # Scale factor güncellemesi
        new_scale = cv2.getTrackbarPos("Scale Factor (x100)", "Ayarlar") / 100.0
        if new_scale != scale_factor:
            scale_factor = new_scale
            frame_width = int(orig_width * scale_factor)
            frame_height = int(orig_height * scale_factor)
            line1_y, line2_y, line3_y = get_lines(frame_height)
            heatmap = np.zeros((frame_height, frame_width), dtype=np.float32)
            cv2.resizeWindow(window_name, frame_width, frame_height)

        if not paused:
            ret, frame = cap.read()
            if not ret:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            if scale_factor != 1.0:
                frame = cv2.resize(frame, (frame_width, frame_height))

        # read trackbar values into a dict for convenience
        tb = {name: cv2.getTrackbarPos(name, "Ayarlar") for name, _, _ in trackbars}
        h_min, h_max = tb["H Min"], tb["H Max"]
        s_min, s_max = tb["S Min"], tb["S Max"]
        v_min, v_max = tb["V Min"], tb["V Max"]
        min_area, max_area = tb["Min Area"], tb["Max Area"]
        min_aspect, max_aspect = tb["Min Aspect"] / 100.0, tb["Max Aspect"] / 100.0
        min_solidity = tb["Min Solidity"] / 100.0
        min_circularity = tb["Min Circularity"] / 100.0
        max_disappeared = tb["Max Disappeared"]
        size_scale = tb["Size Scale (x100)"] / 100.0

        split_enable = tb["Split Enable"]
        split_mult = tb["Split Area Mult (x100)"] / 100.0
        split_dist_thresh = tb["Split Dist Thresh"]

        tracker.maxDisappeared = max_disappeared

        lower_white = np.array([h_min, s_min, v_min], dtype=np.uint8)
        upper_white = np.array([h_max, s_max, v_max], dtype=np.uint8)

        roi_y1 = line1_y
        roi_y2 = line3_y
        band = frame[roi_y1:roi_y2, :].copy()

        if eggs_green_mask is None or eggs_green_mask.shape != band.shape:
            eggs_green_mask = np.zeros_like(band)
        else:
            eggs_green_mask.fill(0)

        # Ön işleme
        blurred = cv2.medianBlur(band, 5)
        hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, lower_white, upper_white)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        # show binary mask in separate window
        cv2.imshow("Mask", mask)

        # Kontur bulma
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Aday kontur listesi (önce split edilecekler, sonra filtrelenecek)
        candidate_contours = []

        for cnt in contours:
            area = cv2.contourArea(cnt)
            # Eğer split aktif ve kontur çok büyükse ayırmayı dene
            if split_enable and area > max_area * split_mult:
                sub_cnts = split_large_contour(cnt, mask, 0, 0, split_dist_thresh)
                candidate_contours.extend(sub_cnts)
            else:
                candidate_contours.append(cnt)

        # Aday konturları filtrele
        rects = []
        n_cand = len(candidate_contours)
        if n_cand > 0:
            areas = np.zeros(n_cand, dtype=np.float32)
            perimeters = np.zeros(n_cand, dtype=np.float32)
            widths = np.zeros(n_cand, dtype=np.int32)
            heights = np.zeros(n_cand, dtype=np.int32)
            xs = np.zeros(n_cand, dtype=np.int32)
            ys = np.zeros(n_cand, dtype=np.int32)
            solidities = np.zeros(n_cand, dtype=np.float32)
            cnt_list = []

            for i, cnt in enumerate(candidate_contours):
                area = cv2.contourArea(cnt)
                areas[i] = area
                perim = cv2.arcLength(cnt, True)
                perimeters[i] = perim
                x, y, w, h = cv2.boundingRect(cnt)
                xs[i] = x
                ys[i] = y
                widths[i] = w
                heights[i] = h
                hull = cv2.convexHull(cnt)
                hull_area = cv2.contourArea(hull)
                solidities[i] = area / hull_area if hull_area > 0 else 0.0
                cnt_list.append(cnt)

            circularities = np.zeros(n_cand, dtype=np.float32)
            for i in range(n_cand):
                if perimeters[i] > 0:
                    circularities[i] = 4 * np.pi * areas[i] / (perimeters[i] * perimeters[i])

            keep = filter_contours(areas, perimeters, widths, heights, solidities, circularities,
                                   min_area, max_area, min_aspect, max_aspect, min_solidity, min_circularity)

            for i in range(n_cand):
                if keep[i]:
                    cnt = cnt_list[i]
                    # Elips uyumu (isteğe bağlı, hız için kapatılabilir)
                    if len(cnt) >= 5:
                        ellipse = cv2.fitEllipse(cnt)
                        ellipse_area = np.pi * (ellipse[1][0]/2.0) * (ellipse[1][1]/2.0)
                        if abs(areas[i] - ellipse_area) / areas[i] > 0.2:
                            continue
                    cv2.drawContours(eggs_green_mask, [cnt], -1, (0, 255, 0), thickness=cv2.FILLED)
                    rects.append((xs[i], ys[i] + roi_y1, xs[i] + widths[i], ys[i] + heights[i] + roi_y1))

        # tracker update and size estimation
        objects = tracker.update(rects) if not paused else tracker.objects

        # map centroids to area; simpler nearest-neighbor
        rect_info = {((r[0]+r[2])//2, (r[1]+r[3])//2): (r[2]-r[0])*(r[3]-r[1]) for r in rects}
        object_sizes = {}
        for objID, (cX, cY) in objects.items():
            # find closest rect
            best = min(rect_info.items(), key=lambda item: (cX-item[0][0])**2 + (cY-item[0][1])**2, default=None)
            if best:
                dist = (cX-best[0][0])**2 + (cY-best[0][1])**2
                if dist < 10000:
                    object_sizes[objID] = best[1] * size_scale

        # Sayım
        if not paused:
            for objectID, centroid in objects.items():
                cX, cY = centroid
                if objectID not in prev_centroids:
                    prev_centroids[objectID] = cY
                    continue
                prev_cY = prev_centroids[objectID]
                if prev_cY < line2_y and cY >= line2_y and objectID not in counted_ids:
                    count += 1
                    counted_ids.add(objectID)
                    if heatmap_update_counter % HEATMAP_UPDATE_FREQ == 0:
                        cv2.circle(heatmap, (cX, cY), 15, (1,), -1)
                prev_centroids[objectID] = cY

        # Görselleştirme (yazılar siyah/beyaz)
        band_with_green = cv2.addWeighted(band, 1.0, eggs_green_mask, 0.5, 0)
        frame[roi_y1:roi_y2, :] = band_with_green

        cv2.line(frame, (0, line1_y), (frame_width, line1_y), (0, 255, 0), 2)
        cv2.line(frame, (0, line2_y), (frame_width, line2_y), (0, 0, 255), 2)
        cv2.line(frame, (0, line3_y), (frame_width, line3_y), (255, 0, 0), 2)

        for objectID, (cX, cY) in objects.items():
            cv2.circle(frame, (cX, cY), 4, (0, 0, 255), -1)
            cv2.putText(frame, str(objectID), (cX - 10, cY - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
            if objectID in object_sizes:
                size_text = f"{object_sizes[objectID]:.1f}"
                cv2.putText(frame, size_text, (cX - 10, cY + 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 1)

        current_time = time.time()
        if not paused and prev_time != 0:
            fps = 1 / (current_time - prev_time)
        else:
            fps = 0
        prev_time = current_time

        cv2.putText(frame, f"Sayim: {count}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        cv2.putText(frame, f"FPS: {fps:.0f}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        if paused:
            cv2.putText(frame, "PAUSED", (frame_width//2 - 100, 100),
                        cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 0, 0), 3)

        # update heatmap overlay periodically
        if heatmap_update_counter % HEATMAP_UPDATE_FREQ == 0:
            norm = cv2.normalize(heatmap, None, 0, 255, cv2.NORM_MINMAX)
            heatmap_color = cv2.applyColorMap(norm.astype(np.uint8), cv2.COLORMAP_JET)
        combined = cv2.addWeighted(frame, 0.7, heatmap_color if 'heatmap_color' in locals() else np.zeros_like(frame), 0.3, 0)

        cv2.imshow(window_name, combined)
        cv2.imshow("Ayarlar", np.zeros((100, 500), np.uint8))

        heatmap_update_counter += 1

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord(' '):
            paused = not paused
        elif key == ord('r'):
            count = 0
            counted_ids.clear()
            prev_centroids.clear()
            heatmap = np.zeros((frame_height, frame_width), dtype=np.float32)
            tracker = CentroidTracker(maxDisappeared=max_disappeared)

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()