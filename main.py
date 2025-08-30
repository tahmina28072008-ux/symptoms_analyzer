import json
import os
import smtplib
from email.message import EmailMessage
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
        doctor_query = doctors_ref.where(filter=firestore.FieldFilter('specialty', '==', specialty))
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
            weekend_query = availability_ref.where(filter=firestore.FieldFilter('doctor_id', '==', doctor_id))\
                                            .where(filter=firestore.FieldFilter('is_booked', '==', False))\
                                            .where(filter=firestore.FieldFilter('time_slot', '>', start_of_weekend))\
                                            .where(filter=firestore.FieldFilter('time_slot', '<', end_of_weekend))\
                                            .order_by('time_slot')\
                                            .limit(1)
            
            weekend_docs = weekend_query.stream()
            appointment_doc = next(weekend_docs, None)

            # 2. If a weekend appointment is not found, fall back to any available appointment within 30 days.
            if not appointment_doc:
                any_day_query = availability_ref.where(filter=firestore.FieldFilter('doctor_id', '==', doctor_id))\
                                                .where(filter=firestore.FieldFilter('is_booked', '==', False))\
                                                .where(filter=firestore.FieldFilter('time_slot', '>', now))\
                                                .where(filter=firestore.FieldFilter('time_slot', '<', thirty_days_from_now))\
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
        doctor_query = doctors_ref.where(filter=firestore.FieldFilter('name', '==', doctor_name)).limit(1)
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
    
def find_user_email(first_name, last_name, dob):
    """
    Looks up a user's email address in the 'patients' Firestore collection
    based on their first name, last name, and date of birth.
    
    Args:
        first_name (str): The user's first name.
        last_name (str): The user's last name.
        dob (str): The user's date of birth.
        
    Returns:
        str: The user's email address if found, otherwise None.
    """
    try:
        patients_ref = db.collection('patients')
        # Updated query to use firestore.FieldFilter to resolve UserWarning.
        user_query = patients_ref.where(filter=firestore.FieldFilter('firstName', '==', first_name))\
                                 .where(filter=firestore.FieldFilter('lastName', '==', last_name))\
                                 .where(filter=firestore.FieldFilter('dob', '==', dob)).limit(1)
        user_docs = user_query.stream()
        user_doc = next(user_docs, None)
        
        if user_doc:
            return user_doc.to_dict().get('email')
        
        return None
    
    except Exception as e:
        print(f"Error finding user: {e}")
        return None

def send_confirmation_email(recipient_email, appointment_details):
    """
    Sends a confirmation email using an SMTP server.
    
    Args:
        recipient_email (str): The email address to send the confirmation to.
        appointment_details (dict): A dictionary with appointment information.
        
    Returns:
        bool: True if the email was sent successfully, False otherwise.
    """
    try:
        # Get SMTP credentials and server details from environment variables.
        # This is the recommended way to handle sensitive information.
        smtp_host = os.environ.get('SMTP_HOST')
        smtp_port = int(os.environ.get('SMTP_PORT', 587))
        smtp_user = os.environ.get('SMTP_USER')
        smtp_pass = os.environ.get('SMTP_PASS')

        if not all([smtp_host, smtp_user, smtp_pass]):
            print("SMTP environment variables are not set. Cannot send email.")
            return False

        # Create the email message.
        msg = EmailMessage()
        msg['Subject'] = 'Your Appointment Confirmation'
        msg['From'] = smtp_user
        msg['To'] = recipient_email
        
        doctor_name = appointment_details.get('doctor_name')
        time_slot = appointment_details.get('time_slot')
        clinic_address = appointment_details.get('clinic_address')
        
        body = f"""
        Hello,

        This email confirms your appointment with Dr. {doctor_name}.

        Appointment Details:
        Date & Time: {time_slot.strftime("%A, %B %d, %Y at %I:%M %p")}
        Location: {clinic_address}

        If you have any questions, please contact the clinic directly.

        Thank you,
        The Healthcare Team
        """
        msg.set_content(body)

        # Connect to the SMTP server and send the email.
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls() # Secure the connection
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            print(f"Confirmation email sent to {recipient_email}")
            return True
            
    except Exception as e:
        print(f"Error sending confirmation email: {e}")
        return False

def book_appointment(doctor_name, time_slot, user_name, user_email):
    """
    Updates the Firestore database to mark a specific appointment as booked
    and creates a new appointment record for the user.
    
    Args:
        doctor_name (str): The name of the doctor.
        time_slot (datetime): The datetime object of the appointment to book.
        user_name (str): The name of the user booking the appointment.
        user_email (str): The email of the user booking the appointment.
        
    Returns:
        bool: True if the booking was successful, False otherwise.
    """
    try:
        # Step 1: Find the doctor's document ID from their name
        doctors_ref = db.collection('doctors')
        doctor_query = doctors_ref.where(filter=firestore.FieldFilter('name', '==', doctor_name)).limit(1)
        doctor_docs = list(doctor_query.stream())

        if not doctor_docs:
            print(f"Doctor not found: {doctor_name}")
            return False
        
        doctor_id = doctor_docs[0].id

        # Step 2: Find the specific time slot document using both doctor_id and time_slot
        availability_ref = db.collection('doctor_availability')
        appointment_query = availability_ref.where(filter=firestore.FieldFilter('doctor_id', '==', doctor_id))\
                                            .where(filter=firestore.FieldFilter('time_slot', '==', time_slot))\
                                            .limit(1)
        
        appointment_docs = list(appointment_query.stream())
        if not appointment_docs:
            print(f"Appointment not found for {doctor_name} at {time_slot}")
            return False

        appointment_doc_ref = appointment_docs[0].reference

        # Step 3: Update the doctor's availability document to mark it as booked.
        appointment_doc_ref.update({'is_booked': True})

        # Step 4: Create a new document in the 'appointments' collection.
        # This logs the appointment for the user's records.
        appointments_ref = db.collection('appointments')
        appointments_ref.add({
            'doctor_name': doctor_name,
            'user_name': user_name,
            'user_email': user_email,
            'time_slot': time_slot,
            'booking_date': datetime.now()
        })
        
        print(f"Appointment booked for {doctor_name} at {time_slot}")
        return True
    
    except Exception as e:
        print(f"Error booking appointment: {e}")
        return False


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
        
        # New parameters to handle doctor selection, insurance, and user details
        selected_doctor_name_choice = session_params.get('selected_doctor_name', None)
        insurance_provider = session_params.get('insurance_provider', None)
        doctor_info_list = session_params.get('doctor_info_list', [])
        
        # New parameters from user input
        user_name = session_params.get('user_name', None)
        dob = session_params.get('dob', None)
        
        # New parameter for booking confirmation
        booking_confirmed = session_params.get('booking_confirmed', False)

        # Initialize doctor object to hold the full details
        selected_doctor_object = None

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
        
        # --- NEW LOGIC FOR BOOKING CONFIRMATION ---
        # This branch is triggered after the user confirms they want to book.
        if booking_confirmed:
            selected_doctor_object = session_params.get('selected_doctor_object')
            
            if selected_doctor_object and user_name and dob:
                # Split the user_name into first and last name to query the database.
                name_parts = user_name.split(' ', 1)
                first_name = name_parts[0]
                last_name = name_parts[1] if len(name_parts) > 1 else ""
                
                # Find the user's email in the database using the updated function.
                user_email = find_user_email(first_name, last_name, dob)

                if user_email:
                    # Convert the Firestore timestamp back to a datetime object
                    time_slot_seconds = selected_doctor_object.get('time_slot', {}).get('_seconds', 0)
                    time_slot_nanos = selected_doctor_object.get('time_slot', {}).get('_nanoseconds', 0)
                    appointment_time = datetime.fromtimestamp(time_slot_seconds + time_slot_nanos / 1e9)
                    
                    appointment_booked = book_appointment(selected_doctor_object.get("name"), appointment_time, user_name, user_email)
                    
                    if appointment_booked:
                        # Call the placeholder function to simulate sending a confirmation email
                        email_sent = send_confirmation_email(user_email, {
                            "doctor_name": selected_doctor_object.get("name"),
                            "time_slot": appointment_time,
                            "clinic_address": selected_doctor_object.get("clinic_address")
                        })
                        if email_sent:
                            response_text = f"Your appointment with {selected_doctor_object.get('name')} has been successfully booked! A confirmation email has been sent to {user_email}."
                        else:
                            response_text = f"Your appointment with {selected_doctor_object.get('name')} has been successfully booked! However, there was an issue sending the confirmation email."
                    else:
                        response_text = "I'm sorry, there was an issue booking your appointment. Please try again."
                else:
                    response_text = "I'm sorry, I could not find your information in the database. Please make sure your name and date of birth are correct."
            else:
                response_text = "I'm sorry, I could not find the doctor's or your details to book the appointment."
            
            # Clear the booking_confirmed flag and other temporary parameters for the next session.
            response = {
                "sessionInfo": {
                    "parameters": {
                        "booking_confirmed": None,
                        "selected_doctor_object": None,
                        "user_name": None,
                        "dob": None
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
        
        # --- ORIGINAL LOGIC FOR INSURANCE CHECK ---
        # This branch is triggered after the user selects a doctor and provides insurance info.
        if selected_doctor_name_choice and insurance_provider:
            # Map the user's choice (e.g., "first doctor") to the actual doctor's name
            try:
                # Find the index of the number word (e.g., "first" -> 0, "second" -> 1)
                number_words = ["first", "second", "third", "fourth", "fifth"]
                choice_index = number_words.index(selected_doctor_name_choice.lower().split()[0])
                if choice_index < len(doctor_info_list):
                    selected_doctor_object = doctor_info_list[choice_index]
                    selected_doctor_name = selected_doctor_object.get("name")
            except (ValueError, IndexError):
                selected_doctor_name = selected_doctor_name_choice
            
            if not selected_doctor_object:
                status, cost, copay = check_insurance_and_cost(selected_doctor_name, insurance_provider)
            else:
                status, cost, copay = check_insurance_and_cost(selected_doctor_object.get("name"), insurance_provider)
            
            # Now, handle the final booking
            if "covered" in status:
                response_text = f"For your visit with {selected_doctor_name}, the status is: Your visit is covered by your insurance. Your estimated copay is: {copay}. Do you want to book this appointment?"
                
                # We save the selected doctor object to the session so we can access it later for booking
                response = {
                    "sessionInfo": {
                        "parameters": {
                            "selected_doctor_object": selected_doctor_object,
                            "final_status": "covered"
                        }
                    }
                    ,
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
            else: # Insurance not covered
                response_text = f"For your visit with {selected_doctor_name}, the status is: {status}"
                if cost:
                    response_text += f"\n\nEstimated total cost: {cost}"
                response_text += f"\n\nWould you like to continue with this booking, or would you prefer to find another doctor who accepts {insurance_provider}?"
                
                response = {
                    "sessionInfo": {
                        "parameters": {
                            "final_status": "not_covered"
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
