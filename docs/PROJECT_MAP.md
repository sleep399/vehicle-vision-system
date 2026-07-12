# Project Map

The single runnable application lives under `database/vehicle-vision-system/`.

## Application root

- `database/vehicle-vision-system/run.py` - application entry point
- `database/vehicle-vision-system/start.bat` - Windows launcher
- `database/vehicle-vision-system/requirements.txt` - Python dependencies
- `database/vehicle-vision-system/.env.example` - environment template
- `database/vehicle-vision-system/README.md` - detailed instructions

## Backend

- `database/vehicle-vision-system/backend/app/main.py` - FastAPI setup
- `database/vehicle-vision-system/backend/app/config.py` - configuration
- `database/vehicle-vision-system/backend/app/database.py` - database and migrations
- `database/vehicle-vision-system/backend/app/schemas.py` - API schemas

### Services

- `backend/app/services/lpr_service.py` - image plate recognition
- `backend/app/services/lpr_video_service.py` - video plate recognition
- `backend/app/services/police_gesture_service.py` - police gestures
- `backend/app/services/owner_gesture_service.py` - owner gestures
- `backend/app/services/alert_agent.py` - monitoring and alerts
- `backend/app/services/llm_service.py` - LLM alert summaries
- `backend/app/services/log_stream.py` - real-time log SSE fan-out
- `backend/app/utils/user_language.py` - user-facing alert explanations and actions

Paths in the service, router, model and utility sections above are relative to
`database/vehicle-vision-system/`.

### Recognition assets

- `yolo_lprnet_assets/` - YOLO and LPRNet runtime
- `backend/app/yolo_lprnet/` - integrated recognition pipeline
- `backend/app/ccpd/` - CCPD-compatible recognition code

Runtime data, uploads, logs, model weights and temporary files are intentionally
excluded from Git.
