import json
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request, jsonify

# Replace 'path/to/your/serviceAccountKey.json' with the actual path to your
# Firebase service account key file. This is required for server-side authentication.
# For Google Cloud deployments, you can use the default credentials.
# Check https://firebase.google.com/docs/admin/setup for more info.
try:
    # Use credentials from a service account file for local development.
    cred = credentials.Certificate('path/to/your/serviceAccountKey.json')
    firebase_admin.initialize_app(cred)
except ValueError:
    # This branch handles the case when the app is running in a GCP environment
    # where credentials are automatically provided.
    firebase_admin.initialize_app()

# Initialize the Firestore database client.
db = firestore.client()

app = Flask(__name__)

def get_available_doctor(specialty):
    """
    Queries Firestore for an available doctor of a specific specialty and their available time slot.
    The query now prioritizes finding a weekend appointment first, then falls back to any day.
    
    Args:
        specialty (str): The medical specialty to search for (e.g., 'gp', 'specialist').
        
    Returns:
        dict: A dictionary containing the doctor's name, clinic address, and an available slot, or None if not found.
    """
    try:
        # Step 1: Find a doctor document by specialty.
        doctors_ref = db.collection('doctors')
        query = doctors_ref.where('specialty', '==', specialty).limit(1)
        
        docs = query.stream()
        
        # Get the first doctor that matches the specialty.
        doctor_doc = next(docs, None)
        
        if not doctor_doc:
            return None # No doctor found for this specialty.
            
        doctor_id = doctor_doc.id
        doctor_data = doctor_doc.to_dict()
        
        # Step 2: Find the next available time slot for this doctor.
        # This requires your 'doctor_availability' documents to have a 'time_slot'
        # field that is a Firestore Timestamp or Python datetime object.
        availability_ref = db.collection('doctor_availability')
        
        # Define a time window for the search (e.g., next 30 days).
        now = datetime.now()
        thirty_days_from_now = now + timedelta(days=30)
        
        # Find the next Saturday and Sunday.
        days_until_saturday = (5 - now.weekday() + 7) % 7
        next_saturday = now + timedelta(days=days_until_saturday)
        start_of_weekend = datetime(next_saturday.year, next_saturday.month, next_saturday.day)
        end_of_weekend = start_of_weekend + timedelta(days=2) # Covers Saturday and Sunday
        
        # 1. First, try to find an available appointment on the upcoming weekend.
        weekend_query = availability_ref.where('doctor_id', '==', doctor_id)\
                                        .where('is_booked', '==', False)\
                                        .where('time_slot', '>', start_of_weekend)\
                                        .where('time_slot', '<', end_of_weekend)\
                                        .order_by('time_slot')\
                                        .limit(1)
        
        weekend_docs = weekend_query.stream()
        appointment_doc = next(weekend_docs, None)

        # 2. If a weekend appointment is not found, fall back to any available appointment within 30 days.
        if not appointment_doc:
            any_day_query = availability_ref.where('doctor_id', '==', doctor_id)\
                                            .where('is_booked', '==', False)\
                                            .where('time_slot', '>', now)\
                                            .where('time_slot', '<', thirty_days_from_now)\
                                            .order_by('time_slot')\
                                            .limit(1)
            any_day_docs = any_day_query.stream()
            appointment_doc = next(any_day_docs, None)
            
        if not appointment_doc:
            return None # No available slots found in either query.
        
        appointment_data = appointment_doc.to_dict()

        # Combine the information into a single result.
        return {
            "name": doctor_data.get('name'),
            "clinic_address": doctor_data.get('clinic_address'),
            "time_slot": appointment_data.get('time_slot')
        }
        
    except Exception as e:
        print(f"Error querying Firestore: {e}")
        return None

@app.route('/', methods=['POST'])
def webhook():
    """
    This function handles the incoming webhook request from Dialogflow CX.
    It processes the user's symptoms and returns a result, now including
    doctor information from Firestore.
    """
    try:
        req = request.get_json(silent=True, force=True)
        print("Webhook Request:")
        print(json.dumps(req, indent=2))

        session_params = req.get('sessionInfo', {}).get('parameters', {})
        symptoms_list = session_params.get('symptoms_list', [])
        symptom_duration_days = session_params.get('symptom_duration_days', 0)

        symptom_result = "self_care"
        symptom_text = ' '.join(symptoms_list).lower()
        
        if "emergency" in symptom_text or "unconscious" in symptom_text or "severe breathing" in symptom_text:
            symptom_result = "emergency"
        elif symptom_duration_days >= 14:
            symptom_result = "specialist"
        elif symptom_duration_days >= 3:
            symptom_result = "gp"
        else:
            symptom_result = "self_care"
        
        # New logic: Look up doctor availability based on the symptom result.
        specialty_map = {
            "gp": "gp",
            "specialist": "specialist"
        }
        
        doctor_info = None
        if symptom_result in specialty_map:
            doctor_info = get_available_doctor(specialty_map[symptom_result])

        # Prepare the webhook response.
        response_text = f"Analyzing your symptoms... Result is: {symptom_result}"
        
        # If a doctor and an appointment were found, include the details in the response.
        if doctor_info:
            appointment_time = doctor_info.get('time_slot')
            formatted_date = appointment_time.strftime("%A, %B %d, %Y")
            formatted_time = appointment_time.strftime("%I:%M %p")
            response_text = f"A doctor is available. We recommend you see a {specialty_map[symptom_result]}. Dr. {doctor_info.get('name')} has an opening on {formatted_date} at {formatted_time} at their clinic on {doctor_info.get('clinic_address')}."
        elif symptom_result in specialty_map:
            response_text = f"There are no available {specialty_map[symptom_result]}s at this time. Please check again later."
        elif symptom_result == "emergency":
            response_text = "Your symptoms indicate an emergency. Please seek immediate medical attention."
        else:
            response_text = "Your symptoms appear to be mild. We recommend self-care measures."

        # This webhook can also set parameters to guide the Dialogflow flow.
        response = {
            "sessionInfo": {
                "parameters": {
                    "symptom_result": symptom_result,
                    "doctor_available": bool(doctor_info),
                    "doctor_name": doctor_info.get('name') if doctor_info else None,
                    "appointment_details": {
                        "name": doctor_info.get('name'),
                        "address": doctor_info.get('clinic_address'),
                        "time": doctor_info.get('time_slot').isoformat() if doctor_info else None
                    } if doctor_info else None
                }
            },
            "fulfillmentResponse": {
                "messages": [
                    {
                        "text": {
                            "text": [response_text]
                        }
                    }
                ]
            }
        }
        
        return jsonify(response)

    except Exception as e:
        print(f"An error occurred: {e}")
        return jsonify({
            "fulfillmentResponse": {
                "messages": [
                    {
                        "text": {
                            "text": ["An error occurred while processing your request."]
                        }
                    }
                ]
            }
        }), 500

if __name__ == '__main__':
    app.run(debug=True, port=8000)
