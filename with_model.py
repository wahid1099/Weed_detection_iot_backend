import os, time, asyncio
from datetime import datetime, timezone
from collections import deque
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Request, Body
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
from dotenv import load_dotenv
import requests
from ultralytics import YOLO
import torch
# ---------- Load environment variables ----------
load_dotenv()
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = os.environ.get("DB_NAME", "iot_weed_ml")

device = "cuda" if torch.cuda.is_available() else "cpu"
MODEL_PATH = "./model/best_custom_model.pt"
yolo_model = YOLO(MODEL_PATH)  # automatically uses CPU or GPU

if not IMGBB_API_KEY:
    raise Exception("Please set IMGBB_API_KEY in environment variables")
if not MONGO_URI:
    raise Exception("Please set MONGO_URI in environment variables")

# ---------- MongoDB Init ----------
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
sensors_col = db["sensors"]
images_col = db["images"]
control_col = db["control"]
telemetry_col = db["telemetry"]

# ---------- FastAPI Init ----------
app = FastAPI()
os.makedirs("uploads", exist_ok=True)

# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, replace * with your Flutter app domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- In-Memory Storage ----------
SENSOR_HISTORY_MAX = 5000
sensor_history = deque(maxlen=SENSOR_HISTORY_MAX)
latest_frame_bytes: Optional[bytes] = None
latest_annotated_path: Optional[str] = None
latest_control = {"cmd": "stop", "speed": 150, "timestamp": datetime.now(timezone.utc).isoformat()}
latest_infer = {"available": False, "timestamp": None, "boxes": [], "score": None, "fname": None}




# ------------------- Weed Inference -------------------
@app.post("/api/infer/weed")
async def infer_weed_simple(image: UploadFile = File(...)):
    try:
        if not image:
            return JSONResponse({"error": "No image sent"}, 400)

        # Save uploaded image
        img_bytes = await image.read()
        fname = f"frame_{int(time.time()*1000)}.jpg"
        path = os.path.join("uploads", fname)
        with open(path, "wb") as f:
            f.write(img_bytes)

        # Run YOLOv8 inference
        results = yolo_model(path)
        weed_detected = False

        # Define which classes are considered "weed"
        WEED_CLASSES = {"weed", "clover", "dandelion", "crabgrass", "thistle"}

        detected_classes = []
        for r in results:
            if r.boxes:
                class_ids = r.boxes.cls.cpu().numpy().tolist()
                for cls_id in class_ids:
                    cls_name = r.names[int(cls_id)].lower()
                    detected_classes.append(cls_name)
                    if cls_name in WEED_CLASSES:
                        weed_detected = True

        # Optionally save annotated image
        import cv2
        annotated_img = results[0].plot()
        annotated_path = f"uploads/annotated_{fname}"
        cv2.imwrite(annotated_path, annotated_img)
        img_url = upload_to_imgbb(annotated_path)

        return {
            "status": "ok",
            "weed_detected": weed_detected,
            "detected_classes": detected_classes,  # show what model saw
            "image_url": img_url
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

# ---------- ImgBB Upload ----------
def upload_to_imgbb(img_path, expiration=None):
    url = f"https://api.imgbb.com/1/upload?key={IMGBB_API_KEY}"
    if expiration:
        url += f"&expiration={expiration}"
    with open(img_path, "rb") as f:
        files = {"image": f}
        response = requests.post(url, files=files)
    if response.status_code == 200:
        data = response.json()
        if data.get("success"):
            return data["data"]["url"]
    print("ImgBB upload failed:", response.text)
    return None

# ------------------- Sensors -------------------
@app.post("/api/sensors")
async def sensors_post(data: dict):
    data["timestamp"] = datetime.utcnow().isoformat()
    sensor_history.append(data)
    sensors_col.insert_one(data)
    return {"status": "ok", "example": {"temperature": 25, "humidity": 40}}

@app.get("/api/sensors/latest")
async def sensors_latest():
    latest = sensors_col.find_one(sort=[("timestamp", -1)])
    if not latest:
        return {"message": "no data"}
    latest["_id"] = str(latest["_id"])
    return latest

@app.get("/api/sensors/history")
async def sensors_history_api(limit: int = 100):
    data = list(sensors_col.find().sort("timestamp", -1).limit(limit))
    for d in data:
        d["_id"] = str(d["_id"])
    return data

# ------------------- Images -------------------
@app.post("/api/images")
async def images_post(image: UploadFile | None = File(None)):
    global latest_frame_bytes
    if image is None:
        return JSONResponse({"error": "No image sent"}, 400)

    img_bytes = await image.read()
    fname = f"frame_{int(time.time() * 1000)}.jpg"
    path = os.path.join("uploads", fname)
    with open(path, "wb") as f:
        f.write(img_bytes)
    latest_frame_bytes = img_bytes

    # Upload to ImgBB
    imgbb_url = upload_to_imgbb(path)
    if not imgbb_url:
        return JSONResponse({"error": "ImgBB upload failed"}, 500)

    # Save metadata to MongoDB
    images_col.insert_one({
        "filename": fname,
        "url": imgbb_url,
        "timestamp": datetime.utcnow().isoformat()
    })

    return {"status": "ok", "filename": fname, "url": imgbb_url}

@app.get("/api/images/latest.jpg")
async def images_latest():
    files = [f for f in os.listdir("uploads") if f.lower().endswith(".jpg")]
    if files:
        newest = max(files, key=lambda f: os.path.getmtime(os.path.join("uploads", f)))
        return FileResponse(os.path.join("uploads", newest), media_type="image/jpeg")
    if latest_frame_bytes:
        return Response(latest_frame_bytes, media_type="image/jpeg")
    return JSONResponse({"message": "no image yet"})

# ------------------- MJPEG Stream -------------------
@app.get("/api/stream")
async def mjpeg_stream():
    async def gen():
        boundary = b"--frame"
        while True:
            await asyncio.sleep(0.05)
            if latest_frame_bytes:
                yield boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + latest_frame_bytes + b"\r\n"
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

# ------------------- Control -------------------
@app.post("/api/control")
async def control_post(body: dict = Body(...)):
    global latest_control
    try:
        cmd = str(body.get("cmd","stop")).lower()
        speed = int(body.get("speed",150))
        if cmd not in ("forward","backward","left","right","stop"):
            return JSONResponse({"error":"invalid cmd"}, 400)
        speed = max(0, min(255, speed))
        latest_control = {"cmd": cmd, "speed": speed, "timestamp": datetime.utcnow().isoformat()}
        result = control_col.insert_one(latest_control)

        # Convert ObjectId to string
        latest_control["_id"] = str(result.inserted_id)
        return {"status":"ok","control":latest_control,
                "example":{"cmd":"forward","speed":150}}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)



@app.get("/api/control/latest")
async def control_latest():
    """Return latest rover control command"""
    return latest_control

# ------------------- Telemetry -------------------
@app.post("/api/telemetry")
async def telemetry_post(data: dict):
    data["timestamp"] = datetime.utcnow().isoformat()
    telemetry_col.insert_one(data)
    print("Telemetry:", data)
    return {"status": "ok"}

# ------------------- ML Inference -------------------
@app.post("/api/infer/run")
async def infer_run():
    if not latest_frame_bytes:
        return JSONResponse({"error": "no frame"}, 400)
    latest_infer.update({
        "available": True,
        "timestamp": datetime.utcnow().isoformat(),
        "boxes": [],
        "score": None,
        "fname": None
    })
    return {"status": "ok", "result": latest_infer}

@app.get("/api/infer/latest")
async def infer_latest():
    return latest_infer

@app.get("/api/images/latest_annotated.jpg")
async def latest_annotated():
    if latest_annotated_path and os.path.exists(latest_annotated_path):
        return FileResponse(latest_annotated_path, media_type="image/jpeg")
    return JSONResponse({"message": "no annotated yet"})

# ------------------- Simple UI -------------------
INDEX = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\"/>
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>RC Weed Rover - Model Test</title>
  <style>
    body {
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #f4f8fb;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
    }
    .container {
      background: #fff;
      margin-top: 60px;
      padding: 32px 28px 24px 28px;
      border-radius: 16px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.08);
      max-width: 400px;
      width: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    h1 {
      color: #2a3b4c;
      margin-bottom: 16px;
      font-size: 2rem;
    }
    .upload-label {
      display: block;
      background: #e3eaf2;
      color: #2a3b4c;
      border-radius: 8px;
      padding: 16px;
      text-align: center;
      cursor: pointer;
      margin-bottom: 16px;
      transition: background 0.2s;
    }
    .upload-label:hover {
      background: #d0d8e0;
    }
    input[type='file'] {
      display: none;
    }
    .preview {
      margin-bottom: 16px;
      max-width: 100%;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
    .btn {
      background: #2a7ade;
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 12px 24px;
      font-size: 1rem;
      cursor: pointer;
      margin-bottom: 12px;
      transition: background 0.2s;
    }
    .btn:disabled {
      background: #b0c4de;
      cursor: not-allowed;
    }
    .result {
      margin-top: 18px;
      font-size: 1.1rem;
      font-weight: 500;
      padding: 12px 18px;
      border-radius: 8px;
      display: inline-block;
    }
    .weed {
      background: #ffeaea;
      color: #d32f2f;
      border: 1px solid #f8bcbc;
    }
    .no-weed {
      background: #eaffea;
      color: #388e3c;
      border: 1px solid #b6eab6;
    }
    .loading {
      color: #2a7ade;
      margin-top: 10px;
    }
    .annotated-img {
      margin-top: 18px;
      max-width: 100%;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
  </style>
</head>
<body>
  <div class=\"container\">
    <h1>Weed Detection Test</h1>
    <label class=\"upload-label\" for=\"file-input\">Choose an image to test the model</label>
    <input id=\"file-input\" type=\"file\" accept=\"image/*\" />
    <img id=\"preview\" class=\"preview\" style=\"display:none\" />
    <button id=\"detect-btn\" class=\"btn\" disabled>Detect Weed</button>
    <div id=\"loading\" class=\"loading\" style=\"display:none\">Detecting...</div>
    <div id=\"result\"></div>
    <img id=\"annotated-img\" class=\"annotated-img\" style=\"display:none\" />
  </div>
  <script>
    const fileInput = document.getElementById('file-input');
    const preview = document.getElementById('preview');
    const detectBtn = document.getElementById('detect-btn');
    const resultDiv = document.getElementById('result');
    const loadingDiv = document.getElementById('loading');
    const annotatedImg = document.getElementById('annotated-img');
    let selectedFile = null;

    fileInput.addEventListener('change', function() {
      const file = this.files[0];
      if (file) {
        selectedFile = file;
        const reader = new FileReader();
        reader.onload = function(e) {
          preview.src = e.target.result;
          preview.style.display = 'block';
        };
        reader.readAsDataURL(file);
        detectBtn.disabled = false;
        resultDiv.innerHTML = '';
        annotatedImg.style.display = 'none';
      } else {
        preview.style.display = 'none';
        detectBtn.disabled = true;
        resultDiv.innerHTML = '';
        annotatedImg.style.display = 'none';
      }
    });

    detectBtn.addEventListener('click', async function() {
      if (!selectedFile) return;
      detectBtn.disabled = true;
      loadingDiv.style.display = 'block';
      resultDiv.innerHTML = '';
      annotatedImg.style.display = 'none';
      const formData = new FormData();
      formData.append('image', selectedFile);
      try {
        const response = await fetch('/api/infer/weed', {
          method: 'POST',
          body: formData
        });
        const data = await response.json();
        loadingDiv.style.display = 'none';
        detectBtn.disabled = false;
        if (data.error) {
          resultDiv.innerHTML = `<span class=\"result weed\">Error: ${data.error}</span>`;
        } else {
          if (data.weed_detected) {
            resultDiv.innerHTML = `<span class=\"result weed\">Weed Detected! 🌱</span>`;
          } else {
            resultDiv.innerHTML = `<span class=\"result no-weed\">No Weed Detected ✔️</span>`;
          }
          if (data.image_url) {
            annotatedImg.src = data.image_url;
            annotatedImg.style.display = 'block';
          }
        }
      } catch (err) {
        loadingDiv.style.display = 'none';
        detectBtn.disabled = false;
        resultDiv.innerHTML = `<span class=\"result weed\">Error: ${err.message}</span>`;
      }
    });
  </script>
</body>
</html>
"""
@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX
