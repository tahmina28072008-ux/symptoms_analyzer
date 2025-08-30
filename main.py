import json
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, request, jsonify

# This try-except block handles credential initialization.
# For local development, it will look for a service account key file.
# When deployed to a Google Cloud service like Cloud Run, it will
# automatically use the default service account credentials
# provided by the environment, making the key file unnecessary.
try:
    # Use credentials from a service account file for local development.
    # Replace 'path/to/your/serviceAccountKey.json' with your actual key's path.
    cred = credentials.Certificate('path/to/your/serviceAccountKey.json')
    firebase_admin.initialize_app(cred)
except ValueError:
    # This branch handles the case when the app is running in a GCP environment
    # where credentials are automatically provided.
    firebase_admin.initialize_app()
except FileNotFoundError:
    # This handles the case where the key file is not found, which is expected
    # when you're deploying to Cloud Run. It will fall back to using default credentials.
    firebase_admin.initialize_app()


# Initialize the Firestore database client.
db = firestore.client()

app = Flask(__name__)

def get_available_doctors(specialty):
    """
    Queries Firestore for all available doctors of a specific specialty and their available time slots.
    The query prioritizes weekend appointments, then falls back to any day.
    
    Args:
        specialty (str): The medical specialty to search for (e.g., 'gp', 'specialist').
        
    Returns:
        list: A list of dictionaries, where each dictionary contains a doctor's name, clinic address, and an available slot. Returns an empty list if no doctors are found.
    """
    try:
        available_doctors = []

        # Step 1: Find a doctor document by specialty.
        doctors_ref = db.collection('doctors')
        doctor_query = doctors_ref.where('specialty', '==', specialty)
        doctor_docs = doctor_query.stream()

        for doctor_doc in doctor_docs:
            doctor_id = doctor_doc.id
            doctor_data = doctor_doc.to_dict()

            # Step 2: Find the next available time slot for this specific doctor.
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
                
            if appointment_doc:
                appointment_data = appointment_doc.to_dict()
                available_doctors.append({
                    "name": doctor_data.get('name'),
                    "clinic_address": doctor_data.get('clinic_address'),
                    "time_slot": appointment_data.get('time_slot')
                })

        return available_doctors
        
    except Exception as e:
        print(f"Error querying Firestore: {e}")
        return []

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
        available_doctors = []
        if symptom_result in specialty_map:
            available_doctors = get_available_doctors(specialty_map[symptom_result])

        # Prepare the webhook response.
        response_text = f"Analyzing your symptoms... Result is: {symptom_result}"
        
        # If a doctor and an appointment were found, include the details in the response.
        if available_doctors:
            response_text = f"We recommend you see a {specialty_map[symptom_result]} based on your symptoms.\n\nAvailable doctors are:\n"
            for doctor in available_doctors:
                appointment_time = doctor.get('time_slot')
                formatted_date = appointment_time.strftime("%A, %B %d, %Y")
                formatted_time = appointment_time.strftime("%I:%M %p")
                response_text += f"Name: {doctor.get('name')}\n"
                response_text += f"Date & Time: {formatted_date} at {formatted_time}\n"
                response_text += f"Location: {doctor.get('clinic_address')}\n\n"
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
                    "doctor_available": bool(available_doctors),
                    "doctor_info_list": available_doctors
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
