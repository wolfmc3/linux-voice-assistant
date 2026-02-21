"""Cheap glance detector abstraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass
class DetectionResult:
    state: str
    confidence: float


class Detector:
    """Detector interface."""

    def analyze(self, frames: Iterable[np.ndarray]) -> DetectionResult:
        raise NotImplementedError


class SimpleFaceGlanceDetector(Detector):
    """Low-cost face presence + rough orientation detector."""

    def __init__(self) -> None:
        self._cascade = None
        try:
            import cv2  # type: ignore

            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self._cascade = cv2.CascadeClassifier(cascade_path)
        except Exception:
            self._cascade = None

    def analyze(self, frames: Iterable[np.ndarray]) -> DetectionResult:
        if self._cascade is None:
            return DetectionResult(state="NO_FACE", confidence=0.0)

        import cv2  # type: ignore

        best_conf = 0.0
        toward_conf = 0.0
        seen_face = False
        for frame in frames:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self._cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=3, minSize=(28, 28))
            if len(faces) == 0:
                continue
            seen_face = True
            h, w = gray.shape
            area_total = float(max(1, w * h))
            for (x, y, fw, fh) in faces:
                face_area = float(fw * fh) / area_total
                cx = x + (fw / 2.0)
                cy = y + (fh / 2.0)
                center_dx = abs((cx / max(1.0, float(w))) - 0.5)
                center_dy = abs((cy / max(1.0, float(h))) - 0.5)
                centered = max(0.0, 1.0 - ((center_dx * 1.8) + (center_dy * 1.2)))
                conf = max(0.0, min(1.0, (face_area * 6.5) + (centered * 0.7)))
                best_conf = max(best_conf, conf)
                toward_conf = max(toward_conf, centered)

        if not seen_face:
            return DetectionResult(state="NO_FACE", confidence=0.0)
        if toward_conf >= 0.45:
            return DetectionResult(state="FACE_TOWARD", confidence=max(best_conf, toward_conf))
        return DetectionResult(state="FACE_AWAY", confidence=max(0.2, min(0.95, best_conf)))

