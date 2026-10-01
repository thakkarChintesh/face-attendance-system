import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
    DB_PATH = os.path.join(BASE_DIR, "attendance.db")

    DATASET_DIR = os.path.join(BASE_DIR, "dataset")          # enrollment photos, one folder per student
    ENCODINGS_FILE = os.path.join(BASE_DIR, "encodings", "faces.pkl")
    UNKNOWN_DIR = os.path.join(BASE_DIR, "static", "unknown")  # snapshots of unknown / spoof faces

    # Recognition defaults (can be overridden from Admin > Settings)
    CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
    THRESHOLD = 0.5          # face distance; lower = stricter
    CONFIRM_FRAMES = 3       # same person must match in N processed frames
    FRAME_SCALE = 0.5        # resize factor before detection (speed)
    PROCESS_EVERY = 2        # process every Nth frame
    DETECTION_MODEL = "hog"  # "hog" on CPU, "cnn" if you have a GPU
    LIVENESS = True          # require a blink before marking
    EAR_THRESHOLD = 0.21     # eye aspect ratio below this = eyes closed
    SPOOF_SECONDS = 8        # recognised but no blink for this long -> log spoof attempt
    PHOTOS_PER_STUDENT = 10
    MIN_ATTENDANCE = 75      # defaulter threshold (%)

    SEED_DEMO = False        # True = create demo users + sample attendance on first run
