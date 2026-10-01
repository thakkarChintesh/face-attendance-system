"""Recognition engine: enrollment and live recognition with blink liveness (dlib + face_recognition + OpenCV).

The camera runs in the browser. The page sends frames here, and we return boxes and names.
"""
import os
import pickle
import threading
import time
import datetime

import cv2
import numpy as np
import face_recognition

from config import Config

_lock = threading.Lock()
RECOGNIZERS = {}  # session_id -> LiveRecognizer


# ---------------------------------------------------------------- encodings
def load_encodings():
    if not os.path.exists(Config.ENCODINGS_FILE):
        return {"ids": [], "encodings": []}
    with open(Config.ENCODINGS_FILE, "rb") as f:
        return pickle.load(f)


def save_encodings(data):
    os.makedirs(os.path.dirname(Config.ENCODINGS_FILE), exist_ok=True)
    with open(Config.ENCODINGS_FILE, "wb") as f:
        pickle.dump(data, f)


def remove_person(user_id):
    with _lock:
        data = load_encodings()
        keep = [i for i, uid in enumerate(data["ids"]) if uid != user_id]
        save_encodings({"ids": [data["ids"][i] for i in keep], "encodings": [data["encodings"][i] for i in keep]})


def enroll_from_folder(user_id, folder):
    """Re-encode every photo in a student's folder. Returns number of usable photos."""
    encs = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        img = face_recognition.load_image_file(os.path.join(folder, name))
        locs = face_recognition.face_locations(img, model=Config.DETECTION_MODEL)
        if len(locs) != 1:          # skip photos with no face or several faces
            continue
        encs.append(face_recognition.face_encodings(img, locs, num_jitters=2)[0])
    with _lock:
        data = load_encodings()
        keep = [i for i, uid in enumerate(data["ids"]) if uid != user_id]
        save_encodings({"ids": [data["ids"][i] for i in keep] + [user_id] * len(encs),
                        "encodings": [data["encodings"][i] for i in keep] + encs})
    return len(encs)


# ---------------------------------------------------------------- liveness
def eye_aspect_ratio(eye):
    p = np.array(eye, dtype="float")
    a = np.linalg.norm(p[1] - p[5])
    b = np.linalg.norm(p[2] - p[4])
    c = np.linalg.norm(p[0] - p[3])
    return (a + b) / (2.0 * c) if c else 0.3


# ---------------------------------------------------------------- live recognition
class LiveRecognizer:
    """Keeps per-session state (match streaks, blinks) across frames sent from the browser."""

    def __init__(self, names, threshold, liveness, already_marked=()):
        data = load_encodings()
        pairs = [(uid, e) for uid, e in zip(data["ids"], data["encodings"]) if uid in names]
        self.known_ids = [p[0] for p in pairs]
        self.known_encs = [p[1] for p in pairs]
        self.names = names
        self.threshold = float(threshold)
        self.liveness = liveness
        self.marked = set(already_marked)
        self.streak, self.closed, self.blinked, self.first_seen, self.spoof_logged = {}, {}, set(), {}, set()
        self.last_unknown = 0.0
        self.lock = threading.Lock()

    def process(self, frame):
        """frame: BGR image. Returns (faces, new_marks, events) where events = [(kind, frame)]."""
        with self.lock:
            scale = Config.FRAME_SCALE
            small = cv2.resize(frame, (0, 0), fx=scale, fy=scale)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            locs = face_recognition.face_locations(rgb, model=Config.DETECTION_MODEL)
            encs = face_recognition.face_encodings(rgb, locs)
            lms = face_recognition.face_landmarks(rgb, locs) if self.liveness else [None] * len(locs)
            now = time.time()
            faces, new_marks, events, seen = [], [], [], set()

            for loc, enc, lm in zip(locs, encs, lms):
                box = [int(v / scale) for v in loc]  # top, right, bottom, left in full-frame pixels
                uid = None
                if self.known_encs:
                    dist = face_recognition.face_distance(self.known_encs, enc)
                    best = int(np.argmin(dist))
                    if dist[best] < self.threshold:
                        uid = self.known_ids[best]

                if uid is None:
                    faces.append({"box": box, "label": "Unknown", "color": "red"})
                    if now - self.last_unknown > 10:
                        self.last_unknown = now
                        events.append(("unknown", frame))
                    continue

                seen.add(uid)
                self.streak[uid] = self.streak.get(uid, 0) + 1
                self.first_seen.setdefault(uid, now)

                if lm:  # blink = eyes closed in >=1 frame, then open again
                    ear = (eye_aspect_ratio(lm["left_eye"]) + eye_aspect_ratio(lm["right_eye"])) / 2
                    if ear < Config.EAR_THRESHOLD:
                        self.closed[uid] = self.closed.get(uid, 0) + 1
                    else:
                        if self.closed.get(uid, 0) >= 1:
                            self.blinked.add(uid)
                        self.closed[uid] = 0

                live_ok = (not self.liveness) or uid in self.blinked
                name = self.names[uid]
                if uid in self.marked:
                    faces.append({"box": box, "label": f"{name} ✓", "color": "green"})
                elif self.streak[uid] >= Config.CONFIRM_FRAMES and live_ok:
                    self.marked.add(uid)
                    new_marks.append(uid)
                    faces.append({"box": box, "label": f"{name} ✓", "color": "green"})
                else:
                    hint = "please blink" if not live_ok else "verifying..."
                    faces.append({"box": box, "label": f"{name} - {hint}", "color": "amber"})
                    if self.liveness and not live_ok and uid not in self.spoof_logged \
                            and now - self.first_seen[uid] > Config.SPOOF_SECONDS:
                        self.spoof_logged.add(uid)
                        events.append(("spoof", frame))

            for k in list(self.streak):
                if k not in seen:
                    self.streak[k] = 0
            return faces, new_marks, events


def decode_jpeg(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def save_snapshot(frame, kind):
    os.makedirs(Config.UNKNOWN_DIR, exist_ok=True)
    name = f"{kind}_{datetime.datetime.now():%Y%m%d_%H%M%S}.jpg"
    cv2.imwrite(os.path.join(Config.UNKNOWN_DIR, name), frame)
    return f"unknown/{name}"
