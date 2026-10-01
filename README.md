# FaceTrack: Facial Recognition Attendance System

Flask, dlib/face_recognition, OpenCV and SQLite, with three role-based dashboards (Admin, Teacher, Student).

## How it works

```
Camera → detect face (HOG) → 68 landmarks → 128-D encoding → compare with enrolled encodings
       → distance < threshold for 3 frames AND blink detected → mark present (once per session)
       → no match → "Unknown" + snapshot to Security log
       → match but never blinks (8 s) → "Spoof attempt" + snapshot
```

## Setup

**macOS:**
```bash
bash setup_mac.sh
source venv/bin/activate
python app.py
```

**Windows / Linux:**
```bash
python -m venv venv
venv\Scripts\activate            # Linux: source venv/bin/activate
pip install -r requirements.txt
pip install --no-deps face_recognition
python app.py
```

Open http://127.0.0.1:5001 in Chrome or Safari and allow camera access when the browser asks.

## Login

On first run only an admin account is created: **`admin` / `admin123`**. Change this password after logging in (Users → Admins → edit).
The admin then creates departments, classes, subjects, teachers and students.

## First real run

1. **Admin → Users → Enroll** each student: open the camera popup and capture 10 photos, or upload photos.
2. **Teacher → Dashboard → Start Attendance** on a lecture. Students look at the camera and blink once.
3. **Stop & Save**, then fix any misses with manual marking (the reason is logged).
4. **Reports**: defaulters, class/subject charts, and Excel/CSV export. Use Print for PDF.

## Features

| Admin | Teacher | Student |
|---|---|---|
| Setup checklist, KPIs, 7/30-day trend, today's present/absent | Today's timetable with pending/completed status | Overall % ring with low-attendance warning |
| Add/edit/delete students and teachers, face enrollment status | Live camera session with names on screen | Subject-wise attended/total |
| Departments, classes, subjects, timetable, teacher assignment | Live present/total count, manual marking | Calendar view and full history |
| All attendance records, manual correction with reason | Per-session roster | Profile with enrolled photo |
| Reports: class, department, subject, monthly, defaulters | Per-student % per subject, defaulter list | Request face data update (admin approves) |
| Security: unknown face and spoof snapshots | Excel/CSV export, low-attendance alerts | Notifications |
| Settings: threshold, camera, liveness, SMTP, backup/restore | | |

## Tuning accuracy (`config.py` / Admin → Settings)

- `THRESHOLD` 0.45–0.50. Lower is stricter (fewer false matches, more misses).
- `CONFIRM_FRAMES = 3` means the same person must match in 3 processed frames.
- `FRAME_SCALE = 0.5`, `PROCESS_EVERY = 2` keep it smooth on a CPU. Use `DETECTION_MODEL = "cnn"` only with a GPU.
- Enroll in classroom lighting, with front and slight side angles, with and without glasses.
- Avoid strong backlight (a window behind students).

## Project structure

```
app.py                 Flask routes, DB schema, reports, demo seed
config.py              Paths and recognition settings
modules/face_engine.py Enrollment, live recognition stream, blink (EAR) liveness
templates/             base layout plus admin/, teacher/, student/ pages
static/css/style.css   UI theme
dataset/               Enrollment photos (one folder per student ID)
encodings/faces.pkl    Saved 128-D encodings
static/unknown/        Unknown/spoof snapshots
```

## Team

| Member | GitHub | Part |
|---|---|---|
| Member 1 | [@thakkarChintesh](https://github.com/thakkarChintesh) | Backend, database, reports |
| Member 2 | [@kevin9826jav](https://github.com/kevin9826jav) | Face recognition engine, enrollment, live attendance |
| Member 3 | [@darkdevilkingofthehell](https://github.com/darkdevilkingofthehell) | Frontend UI and dashboards |
