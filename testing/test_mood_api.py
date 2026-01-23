import requests
import base64
import sys
import os

def test_mood_api(image_path, url='http://localhost:8001/api/mood/detect'):
    if not os.path.exists(image_path):
        print(f"Error: File not found at {image_path}")
        return

    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')

    payload = {
        "image": encoded_string
    }

    try:
        response = requests.post(url, json=payload)
        
        print(f"Status Code: {response.status_code}")
        if response.status_code == 200:
            print("Response:", response.json())
        else:
            print("Error:", response.text)
    except requests.exceptions.ConnectionError:
        print(f"Error: Could not connect to {url}. Is the server running?")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_mood_api.py <path_to_image>")
        sys.exit(1)
    
    image_path = sys.argv[1]
    test_mood_api(image_path)
