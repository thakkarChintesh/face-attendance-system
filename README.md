# FaceTrack: Facial Recognition Attendance System

A web-based attendance system that recognises students by their face. The teacher opens the camera, students look at it and blink once, and their attendance is saved automatically. It has three role-based dashboards for **Admin**, **Teacher** and **Student**.

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | **Python 3.9+** | Core application logic |
| Web framework | **Flask** | Web server, routing, sessions, login |
| Face recognition | **dlib**, **face_recognition** | Face detection, 68 facial landmarks, 128-D face encodings |
| Image processing | **OpenCV (cv2)**, **NumPy** | Decode camera frames, resize, colour conversion, distance math |
| Database | **SQLite** | Users, classes, subjects, sessions, attendance |
| Frontend | **HTML5, CSS3, JavaScript**, **Jinja2** templates | Dashboards and pages |
| Camera access | **WebRTC `getUserMedia`** (browser) | Opens the webcam with a permission popup |
| Charts | **Chart.js** | Attendance trends and report graphs |
| Icons and font | **Lucide Icons**, **Plus Jakarta Sans** | UI icons and typography |
| Reports | **pandas**, **openpyxl** | Excel and CSV export |
| Security | **Werkzeug** (PBKDF2-SHA256) | Password hashing |
| Version control | **Git**, **GitHub** | `dev` / `main` branching workflow |

---

## System Architecture

```
┌─────────────────────────── Browser ───────────────────────────┐
│  Admin / Teacher / Student dashboards (HTML, CSS, JS)          │
│  Camera popup (WebRTC) ── sends JPEG frames ──┐                │
└───────────────────────────────────────────────┼────────────────┘
                                                ▼
┌──────────────────────── Flask server (app.py) ─────────────────┐
│  Login & roles · Admin / Teacher / Student routes · Reports    │
│                     │                                          │
│                     ▼                                          │
│  Face engine (modules/face_engine.py)                          │
│  detect → landmarks → 128-D encoding → match → blink check     │
└─────────────┬──────────────────────────────┬───────────────────┘
              ▼                              ▼
     SQLite (attendance.db)        encodings/faces.pkl, dataset/
```

---

## How Face Recognition Works

```
Camera → Face detection → 68 landmarks → 128-D encoding → Compare → Blink check → Mark present
```

1. **Face detection (HOG).** dlib's *Histogram of Oriented Gradients* detector finds where faces are in the frame. It runs fast on a normal CPU.
2. **68 facial landmarks.** dlib locates 68 points on each face: eyes, eyebrows, nose, lips and jaw.
3. **128-D face encoding.** A deep learning model (ResNet-based CNN, pre-trained by dlib) converts each face into **128 numbers**. Two photos of the same person give very similar numbers; different people give very different numbers.
4. **Matching (Euclidean distance).** The live face's encoding is compared with every enrolled student's encodings. If the smallest distance is **below 0.5** (configurable), it is a match.
5. **3-frame confirmation.** The same student must match in **3 consecutive frames** before anything is saved, which removes random errors.
6. **Liveness check (blink detection).** From the 6 landmark points of each eye the **Eye Aspect Ratio** is calculated:

   ```
   EAR = ( |p2 − p6| + |p3 − p5| ) / ( 2 × |p1 − p4| )
   ```

   When the eye closes, EAR drops below **0.21**. A close followed by an open counts as a blink. A printed photo or a phone screen cannot blink, so it is rejected.
7. **Mark attendance.** Only after a match **and** a blink is the student saved to the `attendance` table, once per session.
8. **Security logging.**
   - **Unknown face:** a snapshot is saved to the Security log (at most one every 10 s).
   - **Spoof attempt:** recognised but no blink for 8 seconds, so a snapshot is saved as a spoof.

### Face enrollment

1. Admin opens **Users → Enroll** for a student and clicks **Open camera**.
2. The browser asks for camera permission and shows the live preview in a popup.
3. 10 photos are captured with prompts to turn the head slightly (left, right, up, down).
4. Each photo with exactly one face is converted to a 128-D encoding and saved in `encodings/faces.pkl`.
5. At least **3 clear photos** are required for a student to count as enrolled.

### Attendance session flow

1. Teacher clicks **Start Attendance**, which creates a new row in `sessions`.
2. The camera popup opens in the browser.
3. JavaScript captures frames and sends them to `POST /api/session/<id>/frame`.
4. The server runs recognition and the blink check, then returns face boxes and names.
5. The browser draws coloured boxes: 🟡 recognised, please blink · 🟢 marked present · 🔴 unknown.
6. The live present count updates every few seconds. The teacher can manually mark anyone the camera missed, and the reason is logged.
7. **Stop & Save** closes the session, and reports update automatically.

---

## Features

| Admin | Teacher | Student |
|---|---|---|
| Totals, today's present/absent, 7/30-day trend | Today's timetable with pending/completed status | Overall % ring with low-attendance warning |
| Add/edit/delete students and teachers | Start attendance with camera popup | Subject-wise attended / total |
| Face enrollment with camera popup or upload | Live names on screen and present/total count | Calendar view and full history |
| Departments, classes, subjects, timetable, teacher assignment | Manual marking with reason | Profile with enrolled photo |
| All attendance records and manual correction | Per-student % per subject, defaulter list | Request face data update (admin approves) |
| Reports: class, department, subject, monthly, defaulters (< 75%) | Excel / CSV export | Notifications |
| Security log of unknown faces and spoof attempts | Low-attendance alerts to students | |
| Settings: threshold, liveness, SMTP email, backup/restore | | |

---

## Database Design

| Table | Key columns | Stores |
|---|---|---|
| `users` | id, username, name, role, password_hash, department_id, class_id, face_enrolled | Admins, teachers, students |
| `departments` | id, name, code | Departments |
| `classes` | id, name, department_id, semester | Classes / semesters |
| `subjects` | id, name, class_id, teacher_id, days, start_time, end_time | Subjects and weekly timetable |
| `sessions` | id, subject_id, teacher_id, date, start_time, end_time, status | One row per lecture |
| `attendance` | id, session_id, student_id, time, method, note | Present records (`UNIQUE(session_id, student_id)`) |
| `unknown_logs` | id, image_path, time, camera, kind | Unknown and spoof snapshots |
| `face_requests` | id, student_id, reason, status | Face update requests |
| `notifications`, `activity`, `settings` | | Alerts, activity log, configurable settings |

Absent students are not stored. They are calculated as *class students minus present students* for each session.

---

## Security & Privacy

- Passwords are stored as **PBKDF2-SHA256 hashes**, never in plain text.
- **Role-based access:** each page checks whether the user is an admin, teacher or student.
- **Blink liveness** blocks photo and screen spoofing.
- Every manual attendance change is recorded with who made it and why.
- Face photos (`dataset/`), encodings (`encodings/`) and the database are **kept local and excluded from Git** (`.gitignore`).

---

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

Open **http://127.0.0.1:5001** in Chrome or Safari and allow camera access when the browser asks.

> `requirements.txt` uses **dlib-bin**, a prebuilt dlib, so no C++ compiler or CMake is needed.

### First login

On first run only an admin account is created: **`admin` / `admin123`**. Change this password right after logging in (Users → Admins → edit).

### Getting started

1. **Academics:** create a department, a class, and subjects with days and times.
2. **Users → Teachers:** add faculty, then assign them to subjects in Academics.
3. **Users → Students:** add students (the Student ID is their login username).
4. **Enroll** each student's face with the camera popup.
5. Teacher logs in → **Start Attendance** → students look at the camera and blink → **Stop & Save**.
6. View **Reports**, export to Excel/CSV, or print to PDF.

---

## Configuration (`config.py` / Admin → Settings)

| Setting | Default | Meaning |
|---|---|---|
| `THRESHOLD` | 0.5 | Match distance. Lower is stricter (fewer false matches, more misses). Recommended 0.45–0.50 |
| `CONFIRM_FRAMES` | 3 | Consecutive frames a student must match |
| `LIVENESS` | on | Require a blink before marking |
| `EAR_THRESHOLD` | 0.21 | Eye Aspect Ratio below this means eyes closed |
| `SPOOF_SECONDS` | 8 | Recognised but no blink for this long counts as a spoof attempt |
| `FRAME_SCALE` | 0.5 | Frames are resized before detection for speed |
| `DETECTION_MODEL` | hog | `hog` for CPU, `cnn` if a GPU is available |
| `MIN_ATTENDANCE` | 75 | Defaulter threshold in % |

**Tips for accuracy:** enroll in the same lighting as the classroom, mix front and slight side angles, take some photos with and without glasses, and avoid strong backlight such as a window behind students. In good conditions accuracy is typically **95–99%**. No face recognition system is 100% accurate, which is why manual marking is available.

---

## Project Structure

```
face_attendance/
├── app.py                  Flask app: routes, database schema, auth, reports, export
├── config.py               Paths and recognition settings
├── requirements.txt        Python dependencies
├── setup_mac.sh            One-step macOS setup
├── modules/
│   └── face_engine.py      Enrollment, live recognition, blink (EAR) liveness
├── templates/
│   ├── base.html           Shared layout: sidebar, top bar
│   ├── login.html
│   ├── session_detail.html Session roster and manual correction
│   ├── admin/              Dashboard, users, enroll, academics, attendance, reports, security, settings
│   ├── teacher/            Dashboard, live attendance, reports
│   └── student/            Dashboard
├── static/
│   ├── css/style.css       UI theme
│   └── unknown/            Unknown / spoof snapshots (local only)
├── dataset/                Enrollment photos, one folder per student (local only)
└── encodings/              faces.pkl with 128-D encodings (local only)
```

---

## Development Workflow

- **`dev`**: all development happens here.
- **`main`**: stable, tested releases. `dev` is merged into `main` through a **Pull Request** after testing.

```
feature work → commit to dev → test → Pull Request dev → main → merge
```

---

## Limitations & Future Scope

**Limitations**
- Accuracy drops in poor lighting, with heavy backlight, or when faces are partly covered (mask, hand).
- Identical twins may be confused.
- Uses a single laptop/USB webcam per session.

**Future scope**
- Mobile app for teachers and students.
- CCTV / IP camera support for automatic classroom-wide attendance.
- GPU-based CNN detector for higher accuracy on large classes.
- SMS / WhatsApp alerts to parents.
- Cloud deployment with a central database for the whole college.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Camera popup says "blocked" | Click the camera icon in the browser address bar → Allow → refresh |
| `pip install dlib` fails | Use the provided `requirements.txt` (it installs prebuilt `dlib-bin`) |
| Port 5001 already in use | Stop the old server with Ctrl + C, or close the old terminal |
| Student not recognised | Re-enroll in better light, or raise the threshold slightly (e.g. 0.55) |
| Wrong person recognised | Lower the threshold (e.g. 0.45) |
| Blink not detected | Move closer to the camera with good front lighting |

---

## Team

| Member | GitHub | Contribution |
|---|---|---|
| Member 1 | [@thakkarChintesh](https://github.com/thakkarChintesh) | Flask backend, database design, authentication, reports and export |
| Member 2 | [@kevin9826jav](https://github.com/kevin9826jav) | Face recognition engine, enrollment and live attendance pages |
| Member 3 | [@darkdevilkingofthehell](https://github.com/darkdevilkingofthehell) | Frontend UI: theme, layout, admin, teacher and student dashboards |
