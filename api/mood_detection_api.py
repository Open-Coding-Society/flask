from flask import Blueprint, request, jsonify

import base64
import cv2
import numpy as np
import os
import tempfile

from flask_cors import CORS, cross_origin


mood_detection_api = Blueprint('mood_detection_api', __name__, url_prefix='/api/mood')
CORS(mood_detection_api, supports_credentials=True, resources={r"/detect": {"origins": "*"}})

@mood_detection_api.route('/detect', methods=['POST'])
@cross_origin(origins="*", supports_credentials=True)

def detect_mood():
    """
    Detects the mood of a person in the provided image.
    Expects a JSON payload with a 'image' key containing a base64 encoded image.
    """
    try:
        data = request.get_json()
        if not data or 'image' not in data:
            return jsonify({'error': 'No image data provided'}), 400

        base64_image = data['image']
        
        # Remove header if present (e.g., "data:image/jpeg;base64,")
        if ',' in base64_image:
            base64_image = base64_image.split(',')[1]

        # Decode base64 string
        try:
            image_bytes = base64.b64decode(base64_image)
        except Exception as e:
            return jsonify({'error': f'Invalid base64 string: {str(e)}'}), 400

        # Create a temporary file to save the image
        # DeepFace analyze usually takes a path or numpy array. 
        # Using a temp file is safer for format detection.
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_file:
            temp_file.write(image_bytes)
            temp_file_path = temp_file.name

        try:
            # Analyze the image using DeepFace
            # actions=['emotion'] to only check for emotion
            from deepface import DeepFace
            analysis = DeepFace.analyze(img_path=temp_file_path, actions=['emotion'], enforce_detection=False)
            
            # DeepFace.analyze returns a list of dictionaries if multiple faces are found, or a single dict (in older versions, but list in newer)
            # We will handle both cases
            result = analysis[0] if isinstance(analysis, list) else analysis
            
            dominant_emotion = result['dominant_emotion']
            emotion_probabilities = result['emotion']

            # Convert numpy types to native Python types
            if isinstance(emotion_probabilities, dict):
                emotion_probabilities = {k: float(v) for k, v in emotion_probabilities.items()}
            
            return jsonify({
                'mood': dominant_emotion,
                # 'probabilities': emotion_probabilities
            }), 200

        except Exception as e:
             import traceback
             traceback.print_exc()
             return jsonify({'error': f'Error analyzing image: {str(e)}'}), 500
        finally:
            # Clean up the temporary file
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500
