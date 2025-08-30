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

def check_insurance_and_cost(doctor_name, insurance_provider):
    """
    Checks a doctor's accepted insurance plans from Firestore and returns the coverage status.
    """
    try:
        doctors_ref = db.collection('doctors')
        doctor_query = doctors_ref.where('name', '==', doctor_name).limit(1)
        doctor_docs = doctor_query.stream()
        doctor_doc = next(doctor_docs, None)

        if not doctor_doc:
            return "Sorry, I can't find information for that doctor.", None, None

        doctor_data = doctor_doc.to_dict()
        accepted_insurances = doctor_data.get("accepted_insurances", [])
        
        # Hardcoded costs for demonstration. In a real app, this would be dynamic.
        visit_cost = "$50"
        copay = "$25"

        if insurance_provider in accepted_insurances:
            return "Your visit is covered by your insurance.", visit_cost, copay
        else:
            return "This doctor does not accept your insurance.", visit_cost, None

    except Exception as e:
        print(f"Error checking insurance: {e}")
        return "An error occurred while checking insurance.", None, None


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
        
        # New parameters to handle doctor selection and insurance check
        selected_doctor_name = session_params.get('selected_doctor_name', None)
        insurance_provider = session_params.get('insurance_provider', None)
        doctor_info_list = session_params.get('doctor_info_list', [])

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
        
        # --- NEW LOGIC FOR INSURANCE CHECK ---
        # This branch is triggered after the user selects a doctor and provides insurance info.
        if selected_doctor_name and insurance_provider:
            # Map the user's choice (e.g., "second doctor") to the actual doctor's name
            try:
                # Find the index of the number word (e.g., "first" -> 0, "second" -> 1)
                number_words = ["first", "second", "third", "fourth", "fifth"]
                choice_index = number_words.index(selected_doctor_name.lower().split()[0])
                if choice_index < len(doctor_info_list):
                    # Update selected_doctor_name to the actual name from the list
                    selected_doctor_name = doctor_info_list[choice_index].get("name")
            except (ValueError, IndexError):
                # If the user's choice is not a number word (e.g., they said the name directly),
                # we don't need to do anything.
                pass

            status, cost, copay = check_insurance_and_cost(selected_doctor_name, insurance_provider)
            response_text = f"For your visit with {selected_doctor_name}, the status is: {status}"
            if cost:
                response_text += f"\n\nEstimated total cost: {cost}"
            if copay:
                response_text += f"\nYour estimated copay is: {copay}"
            
            # Return this response immediately, skipping the symptom analysis.
            response = {
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
        
        # --- ORIGINAL SYMPTOM ANALYSIS LOGIC ---
        # This is the initial flow to analyze symptoms and find doctors.
        specialty_map = {
            "gp": "gp",
            "specialist": "specialist"
        }
        
        available_doctors = []
        if symptom_result in specialty_map:
            available_doctors = get_available_doctors(specialty_map[symptom_result])

        # Prepare the webhook response for symptom analysis.
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
