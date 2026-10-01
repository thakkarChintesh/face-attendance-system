#!/bin/bash
# One-time setup for macOS. Run from the face_attendance folder:  bash setup_mac.sh
set -e
cd "$(dirname "$0")"
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install --no-deps face_recognition   # uses the prebuilt dlib-bin instead of compiling dlib
python -c "import dlib, face_recognition, cv2, flask; print('\nSETUP OK - dlib', dlib.__version__)"
