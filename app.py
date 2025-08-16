import os, time, asyncio
from datetime import datetime
from collections import deque
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Request, Body
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response, FileResponse
from dotenv import load_dotenv
import requests
from pymongo import MongoClient
from datetime import timezone
from fastapi.middleware.cors import CORSMiddleware




# ---------- Load env ----------
load_dotenv()
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = os.environ.get("DB_NAME", "iot_weed_ml")




if not IMGBB_API_KEY:
    raise Exception("Please set IMGBB_API_KEY in your environment variables")
if not MONGO_URI:
    raise Exception("Please set MONGO_URI in your environment variables")

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
    allow_origins=[
        "*", 
        "http://localhost",
        "http://localhost:3000",
        "http://127.0.0.1",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ---------- In-Memory Storage ----------
SENSOR_HISTORY_MAX = 5000
sensor_history = deque(maxlen=SENSOR_HISTORY_MAX)
latest_frame_bytes: Optional[bytes] = None
latest_control = {"cmd":"stop", "speed":150, "timestamp": datetime.now(timezone.utc).isoformat()}
latest_infer = {"available": False, "timestamp": None, "boxes": [], "score": None, "fname": None}
latest_annotated_path: Optional[str] = None

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
    """Add a sensor reading"""
    data["timestamp"] = datetime.utcnow().isoformat()
    sensor_history.append(data)
    sensors_col.insert_one(data)
    return {"status":"ok", "example": {"temperature":25, "humidity":40}}



@app.get("/api/sensors/latest")
async def sensors_latest():
    """Get latest sensor reading from MongoDB"""
    latest = sensors_col.find_one(sort=[("timestamp", -1)])
    if not latest:
        return {"message":"no data"}
    latest["_id"] = str(latest["_id"])  # convert ObjectId to string
    return latest

@app.get("/api/sensors/history")
async def sensors_history_api(limit: int = 100):
    """Get last N sensor readings from MongoDB"""
    data = list(sensors_col.find().sort("timestamp", -1).limit(limit))
    for d in data:
        d["_id"] = str(d["_id"])
    return data


# ------------------- Images -------------------
@app.post("/api/images")
async def images_post(image: UploadFile | None = File(None)):
    """Upload an image to ImgBB"""
    global latest_frame_bytes
    if image is None:
        return JSONResponse({"error": "No image sent"}, 400)

    img_bytes = await image.read()
    fname = f"frame_{int(time.time()*1000)}.jpg"
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

    return {"status":"ok", "filename": fname, "url": imgbb_url,
            "example": "curl -F 'image=@file.jpg' http://localhost:8000/api/images"}

@app.get("/api/images/latest.jpg")
async def images_latest():
    """Get latest uploaded image"""
    files = [f for f in os.listdir("uploads") if f.lower().endswith(".jpg")]
    if files:
        newest = max(files, key=lambda f: os.path.getmtime(os.path.join("uploads", f)))
        return FileResponse(os.path.join("uploads", newest), media_type="image/jpeg")
    if latest_frame_bytes:
        return Response(latest_frame_bytes, media_type="image/jpeg")
    return JSONResponse({"message":"no image yet"})

# ------------------- MJPEG Stream -------------------
@app.get("/api/stream")
async def mjpeg_stream():
    """MJPEG live stream"""
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
    """Send control command"""
    global latest_control
    cmd = str(body.get("cmd","stop")).lower()
    speed = int(body.get("speed",150))
    if cmd not in ("forward","backward","left","right","stop"):
        return JSONResponse({"error":"invalid cmd"}, 400)
    speed = max(0, min(255, speed))
    latest_control = {"cmd": cmd, "speed": speed, "timestamp": datetime.utcnow().isoformat()}
    control_col.insert_one(latest_control)
    return {"status":"ok","control":latest_control,
            "example":{"cmd":"forward","speed":150}}

@app.get("/api/control/latest")
async def control_latest():
    """Get latest control command"""
    return latest_control

# ------------------- Telemetry -------------------
@app.post("/api/telemetry")
async def telemetry_post(data: dict):
    """Send telemetry data"""
    data["timestamp"] = datetime.utcnow().isoformat()
    telemetry_col.insert_one(data)
    print("TEL:", data)
    return {"status": "ok", "example":{"battery":90, "gps":{"lat":23,"lon":90}}}

# ------------------- ML Inference -------------------
@app.post("/api/infer/run")
async def infer_run():
    """Run ML inference on latest frame (stub)"""
    if not latest_frame_bytes:
        return JSONResponse({"error":"no frame"},400)
    latest_infer.update({"available": True, "timestamp": datetime.utcnow().isoformat(),
                         "boxes": [], "score": None, "fname": None})
    return {"status":"ok","result":latest_infer}

@app.get("/api/infer/latest")
async def infer_latest():
    """Get latest inference result"""
    return latest_infer

@app.get("/api/images/latest_annotated.jpg")
async def latest_annotated():
    if latest_annotated_path and os.path.exists(latest_annotated_path):
        return FileResponse(latest_annotated_path, media_type="image/jpeg")
    return JSONResponse({"message":"no annotated yet"})

# ------------------- Simple UI -------------------
INDEX = """
<!doctype html><html><head><meta charset="utf-8"/>
<title>RC Weed Rover</title></head>
<body>
<h1>RC Weed Rover</h1>
<form action="/api/images" method="post" enctype="multipart/form-data">
    <input type="file" name="image">
    <input type="submit" value="Upload">
</form>
<p>Use /api/sensors, /api/control, /api/telemetry endpoints as JSON POST requests.</p>
</body></html>
"""
@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX
