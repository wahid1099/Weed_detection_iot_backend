import requests

# API endpoint
url = "http://127.0.0.1:8000/api/infer/weed"  # if running locally

# Image to test
image_path = "uploads/test2.jpg"  # replace with your image path

# Send POST request
with open(image_path, "rb") as f:
    files = {"image": f}
    response = requests.post(url, files=files)

# Print response
print("Status code:", response.status_code)
print("Response JSON:", response.json())
