import json
import os
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore, exceptions
from flask import Flask, request, jsonify
import re # Import the regular expression module

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

# --- Helper Functions for Database Interaction and Email ---

def _get_date_string_from_dob_param(dob_param):
    """
    Helper function to robustly parse the date from a Dialogflow parameter.
    It handles simple strings, structured dictionary formats, and Firestore Timestamps.
    """
    if isinstance(dob_param, str):
        # Handle simple MM/DD/YYYY strings
        try:
            dob_obj = datetime.strptime(dob_param, "%m/%d/%Y")
            return dob_obj.strftime("%Y-%m-%d")
        except ValueError:
            # Handle standard ISO 8601 format from Dialogflow entities
            try:
                dob_obj = datetime.strptime(dob_param, "%Y-%m-%dT%H:%M:%SZ")
                return dob_obj.strftime("%Y-%m-%d")
            except ValueError:
                return None
    elif isinstance(dob_param, dict) and 'year' in dob_param and 'month' in dob_param and 'day' in dob_param:
        try:
            # Reconstruct the date from the dictionary and format it as YYYY-MM-DD
            year = int(dob_param['year'])
            month = int(dob_param['month'])
            day = int(dob_param['day'])
            return f"{year:04d}-{month:02d}-{day:02d}"
        except (ValueError, TypeError):
            return None
    
    return None

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
        now = datetime.now()
        thirty_days_from_now = now + timedelta(days=30)

        # Step 1: Find all doctors with the specified specialty.
        doctors_ref = db.collection('doctors')
        doctor_query = doctors_ref.where(filter=firestore.FieldFilter('specialty', '==', specialty))
        doctor_docs = doctor_query.stream()

        for doctor_doc in doctor_docs:
            doctor_id = doctor_doc.id
            doctor_data = doctor_doc.to_dict()
            
            # Step 2: For each doctor, find the next available appointment.
            availability_ref = db.collection('doctor_availability')
            
            # First, try to find an appointment on the upcoming weekend.
            days_until_saturday = (5 - now.weekday() + 7) % 7
            next_saturday = now + timedelta(days=days_until_saturday)
            start_of_weekend = datetime(next_saturday.year, next_saturday.month, next_saturday.day)
            end_of_weekend = start_of_weekend + timedelta(days=2) # Covers Saturday and Sunday
            
            weekend_query = availability_ref.where(filter=firestore.FieldFilter('doctor_id', '==', doctor_id)) \
                                        .where(filter=firestore.FieldFilter('is_booked', '==', False)) \
                                        .where(filter=firestore.FieldFilter('time_slot', '>=', start_of_weekend)) \
                                        .where(filter=firestore.FieldFilter('time_slot', '<', end_of_weekend)) \
                                        .order_by('time_slot').limit(1)
            
            appointment_doc = next(weekend_query.stream(), None)

            # If no weekend slot is found, search for any available slot within the next 30 days.
            if not appointment_doc:
                any_day_query = availability_ref.where(filter=firestore.FieldFilter('doctor_id', '==', doctor_id)) \
                                             .where(filter=firestore.FieldFilter('is_booked', '==', False)) \
                                             .where(filter=firestore.FieldFilter('time_slot', '>', now)) \
                                             .where(filter=firestore.FieldFilter('time_slot', '<', thirty_days_from_now)) \
                                             .order_by('time_slot').limit(1)
                appointment_doc = next(any_day_query.stream(), None)
            
            if appointment_doc:
                appointment_data = appointment_doc.to_dict()
                appointment_data['id'] = appointment_doc.id # Save the document ID for booking
                
                # Add the doctor's name and clinic address to the availability data
                # for easier access later in the flow.
                appointment_data['name'] = doctor_data.get('name')
                appointment_data['clinic_address'] = doctor_data.get('clinic_address')
                available_doctors.append(appointment_data)

        return available_doctors
        
    except Exception as e:
        print(f"Error querying Firestore for doctors: {e}")
        return []

def check_insurance_and_cost(doctor_name, insurance_provider):
    """
    Checks if a specific doctor accepts a given insurance provider.

    Args:
        doctor_name (str): The name of the doctor.
        insurance_provider (str): The name of the insurance provider.

    Returns:
        tuple: A tuple containing the coverage status message (str), the estimated
                visit cost (str), and the estimated copay (str).
    """
    try:
        doctors_ref = db.collection('doctors')
        doctor_query = doctors_ref.where(filter=firestore.FieldFilter('name', '==', doctor_name)).limit(1)
        doctor_docs = list(doctor_query.stream())

        if not doctor_docs:
            return "Sorry, I can't find information for that doctor.", None, None

        doctor_data = doctor_docs[0].to_dict()
        accepted_insurances = doctor_data.get("accepted_insurances", [])
        
        # Hardcoded costs for demonstration. In a real application, this would be
        # based on a more detailed lookup or a payment API.
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
        dob (str): The user's date of birth in 'YYYY-MM-DD' format.

    Returns:
        str: The user's email address if found, otherwise None.
    """
    try:
        patients_ref = db.collection('patients')
        user_query = patients_ref.where(filter=firestore.FieldFilter('firstName', '==', first_name))\
                                 .where(filter=firestore.FieldFilter('lastName', '==', last_name))\
                                 .where(filter=firestore.FieldFilter('dob', '==', dob)).limit(1)
        user_docs = user_query.stream()
        user_doc = next(user_docs, None)
        
        if user_doc:
            return user_doc.to_dict().get('email')
        
        return None
    
    except exceptions.FirebaseError as e:
        print(f"Firestore query failed: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred while finding user email: {e}")
        return None

def send_confirmation_email(recipient_email, appointment_details):
    """
    Sends a confirmation email using SMTP.

    Args:
        recipient_email (str): The email address to send the confirmation to.
        appointment_details (dict): A dictionary with appointment information.

    Returns:
        bool: True if the email was sent successfully, False otherwise.
    """
    try:
        # Get SMTP credentials and server details from environment variables.
        smtp_host = os.environ.get('SMTP_HOST')
        smtp_port = int(os.environ.get('SMTP_PORT', 587))
        smtp_user = os.environ.get('SMTP_USER')
        smtp_pass = os.environ.get('SMTP_PASS')

        if not all([smtp_host, smtp_user, smtp_pass]):
            print("SMTP environment variables are not set. Cannot send email.")
            return False

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

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            print(f"Confirmation email sent to {recipient_email}")
            return True
            
    except Exception as e:
        print(f"Error sending confirmation email: {e}")
        return False

def book_appointment(appointment_doc_id, user_name, user_email, selected_doctor_object, symptoms):
    """
    Updates the Firestore database to mark a specific appointment as booked
    and creates a new appointment record for the user.

    Args:
        appointment_doc_id (str): The document ID of the specific time slot to book.
        user_name (str): The name of the user booking the appointment.
        user_email (str): The email of the user booking the appointment.
        selected_doctor_object (dict): The full doctor and appointment object.
        symptoms (list): The list of symptoms provided by the user.

    Returns:
        bool: True if the booking was successful, False otherwise.
    """
    print(f"Attempting to book appointment with ID: {appointment_doc_id}")
    try:
        # Get the reference to the specific appointment document.
        appointment_doc_ref = db.collection('doctor_availability').document(appointment_doc_id)
        
        # Check if the document exists and is not already booked.
        doc_snapshot = appointment_doc_ref.get()
        if not doc_snapshot.exists or doc_snapshot.get('is_booked'):
            print("Appointment is either non-existent or already booked.")
            return False

        # Use a Firestore transaction to ensure atomic operations.
        @firestore.transactional
        def update_in_transaction(transaction, appointment_ref):
            # Read the document inside the transaction.
            snapshot = appointment_ref.get(transaction=transaction)
            if not snapshot.exists or snapshot.get('is_booked'):
                raise ValueError("Appointment already booked by another user or does not exist.")
            
            # Update the availability document to prevent double-booking.
            transaction.update(appointment_ref, {'is_booked': True})

        # Run the transaction.
        transaction = db.transaction()
        update_in_transaction(transaction, appointment_doc_ref)
        print("Appointment document updated successfully.")

        # Create a new document in the 'appointments' collection.
        appointments_ref = db.collection('appointments')
        
        # Create a new document with all the booking details.
        new_appointment_data = {
            'patient_name': user_name,
            'patient_email': user_email,
            'doctor_name': selected_doctor_object.get('name'),
            'clinic_address': selected_doctor_object.get('clinic_address'),
            'time_slot': selected_doctor_object.get('time_slot'),
            'symptoms': symptoms,
            'booking_date': datetime.now()
        }
        
        appointments_ref.add(new_appointment_data)
        print(f"Appointment record created for user {user_name}.")
        return True
    
    except exceptions.FirebaseError as e:
        print(f"Firestore transaction failed: {e}")
        return False
    except ValueError as e:
        print(f"Error during transaction: {e}")
        return False
    except Exception as e:
        print(f"Error booking appointment: {e}")
        return False

def get_doctor_from_choice(doctor_info_list, choice_string):
    """
    A robust helper function to find the doctor object based on the user's choice.
    It can handle both numerical choices (e.g., "first one") and direct names (e.g., "Dr. Jane Doe").
    
    Args:
        doctor_info_list (list): The list of doctor objects stored in the session.
        choice_string (str): The user's input string.
        
    Returns:
        dict: The matching doctor object, or None if not found.
    """
    choice_lower = choice_string.lower()

    # Map number words to their indices.
    number_words = ["first", "second", "third", "fourth", "fifth"]
    for i, word in enumerate(number_words):
        if word in choice_lower:
            try:
                # Return the doctor at the corresponding index.
                return doctor_info_list[i]
            except IndexError:
                # Handle cases where the number is out of the list's bounds.
                return None
    
    # Try to extract a name using a simple regex and match it to the list
    match = re.search(r'dr\.?\s+([a-zA-Z\s]+)', choice_string, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        # Find the doctor object where the name matches
        return next((d for d in doctor_info_list if name.lower() in d.get('name', '').lower()), None)
    
    return None

# --- Main Webhook Endpoint ---

@app.route('/', methods=['POST'])
def webhook():
    """
    This function handles the incoming webhook request from Dialogflow CX.
    It processes the user's symptoms, finds a doctor, checks insurance, and
    handles the final appointment booking.
    """
    try:
        req = request.get_json(silent=True, force=True)
        print("Webhook Request:")
        print(json.dumps(req, indent=2))

        # Safely extract parameters from the session.
        session_params = req.get('sessionInfo', {}).get('parameters', {})
        print(f"Webhook received parameters: {session_params}")
        print("-" * 50)
        
        symptoms_list = session_params.get('symptoms_list', [])
        symptom_duration_days = session_params.get('symptom_duration_days', 0)
        
        # Parameters for doctor selection and booking flow
        selected_doctor_choice = session_params.get('selected_doctor_name', None)
        insurance_provider = session_params.get('insurance_provider', None)
        doctor_info_list = session_params.get('doctor_info_list', [])
        
        # Parameters for user identification and booking confirmation
        user_name = session_params.get('user_name', None)
        dob = session_params.get('dob', None)
        booking_confirmed = session_params.get('booking_confirmed', False)
        
        # --- Logic for Appointment Confirmation ---
        if booking_confirmed:
            print("Entering appointment confirmation logic block...")
            # We will use the selected_doctor_object directly from the session.
            selected_doctor_object = session_params.get('selected_doctor_object', {})
            
            # This is a critical debugging check!
            print(f"Attempting to book: selected_doctor_object={selected_doctor_object}, user_name={user_name}, dob={dob}")
            
            if selected_doctor_object and user_name and dob:
                name_parts = user_name.split(' ', 1)
                first_name = name_parts[0]
                last_name = name_parts[1] if len(name_parts) > 1 else ""
                
                # Use the new helper function to get a clean date string.
                formatted_dob = _get_date_string_from_dob_param(dob)

                if not formatted_dob:
                    response_text = "I'm sorry, the date of birth format is incorrect. Please use MM/DD/YYYY."
                    return jsonify({
                        "fulfillmentResponse": {
                            "messages": [{"text": {"text": [response_text]}}]
                        }
                    })

                user_email = find_user_email(first_name, last_name, formatted_dob)

                if user_email:
                    # The `id` is a string, which is what we need.
                    appointment_doc_id = selected_doctor_object.get('id')
                    
                    if appointment_doc_id:
                        appointment_booked = book_appointment(
                            appointment_doc_id, 
                            user_name, 
                            user_email, 
                            selected_doctor_object, 
                            symptoms_list # Pass the symptoms
                        )
                    
                        if appointment_booked:
                            # The `time_slot` here is a Firestore Timestamp, so we can use it directly.
                            time_slot = selected_doctor_object.get('time_slot')
                            
                            email_sent = send_confirmation_email(user_email, {
                                "doctor_name": selected_doctor_object.get("name"),
                                "time_slot": time_slot,
                                "clinic_address": selected_doctor_object.get("clinic_address")
                            })
                            
                            if email_sent:
                                response_text = f"Your appointment with Dr. {selected_doctor_object.get('name')} has been successfully booked! A confirmation email has been sent to {user_email}."
                            else:
                                response_text = f"Your appointment with Dr. {selected_doctor_object.get('name')} has been booked! However, there was an issue sending the confirmation email."
                        else:
                            response_text = "I'm sorry, that appointment time is no longer available. Please try again."
                    else:
                        response_text = "I'm sorry, I could not find the appointment details. Please try again."
                else:
                    response_text = "I'm sorry, I could not find your information in the database. Please ensure your name and date of birth are correct."
            else:
                response_text = "I'm sorry, I could not find the necessary details to book your appointment. Please restart the process."
            
            # This is the final response. It resets the temporary session parameters.
            response = {
                "sessionInfo": {
                    "parameters": {
                        "booking_confirmed": None,
                        "selected_doctor_object": None,
                        "user_name": None,
                        "dob": None,
                        "symptoms_list": None, # Clear this too
                        # Crucial fix: Send back the parameters for Dialogflow's final fulfillment message
                        "doctor_name": selected_doctor_object.get('name') if selected_doctor_object else None,
                        "appointment_time": selected_doctor_object.get('time_slot').strftime('%a, %d %b %Y %H:%M:%S GMT') if selected_doctor_object and selected_doctor_object.get('time_slot') else None
                    }
                },
                "fulfillmentResponse": {
                    "messages": [{"text": {"text": [response_text]}}]
                }
            }
            return jsonify(response)

        # --- Logic for Insurance Check ---
        # This branch is triggered when the user selects a doctor and provides an insurance provider.
        if selected_doctor_choice and insurance_provider and doctor_info_list:
            print("Entering insurance check logic block...")
            # This is a critical debugging check!
            print(f"Doctor Choice: {selected_doctor_choice}, Insurance: {insurance_provider}")
            
            # Use the new, more robust helper function to find the doctor object
            selected_doctor_object = get_doctor_from_choice(doctor_info_list, selected_doctor_choice)

            if selected_doctor_object:
                selected_doctor_name = selected_doctor_object.get("name")
                status, cost, copay = check_insurance_and_cost(selected_doctor_name, insurance_provider)
                
                # We save the selected doctor object and the full list to the session.
                response_params = {
                    "selected_doctor_object": selected_doctor_object,
                    "final_status": "covered" if "covered" in status else "not_covered",
                    "doctor_info_list": doctor_info_list # Corrected: Pass the list back to the session
                }
                
                if "covered" in status:
                    response_text = f"Your visit is covered by your insurance. Your estimated copay is: {copay}. Do you want to book this appointment with Dr. {selected_doctor_name}?"
                else:
                    response_text = f"This doctor does not accept your insurance. Estimated total cost: {cost}. Would you like to book this appointment anyway?"
                
                response = {
                    "sessionInfo": {"parameters": response_params},
                    "fulfillmentResponse": {"messages": [{"text": {"text": [response_text]}}]}
                }
                return jsonify(response)
            else:
                # If we couldn't find the doctor, send an error message back to the user
                response_text = "I'm sorry, I couldn't find that doctor in my records. Can you please select one by number (e.g., 'first one') or type the full name exactly as it appears?"
                response = {
                    "fulfillmentResponse": {"messages": [{"text": {"text": [response_text]}}]}
                }
                return jsonify(response)
        
        # --- Logic for Initial Symptom Analysis ---
        # This is the initial flow to analyze symptoms and find doctors.
        symptom_text = ' '.join(symptoms_list).lower()
        
        # Determine the medical outcome based on symptoms and duration.
        if "emergency" in symptom_text or "severe breathing" in symptom_text or "unconscious" in symptom_text:
            symptom_result = "emergency"
            response_text = "Your symptoms may be a medical emergency. Please seek immediate medical attention by calling 999 or going to your nearest A&E."
            
        elif symptom_duration_days >= 14:
            symptom_result = "specialist"
            response_text = "Based on the persistence of your symptoms for more than 14 days, we recommend you book an appointment with a specialist."
        
        elif symptom_duration_days >= 3:
            symptom_result = "gp"
            response_text = "Given your symptoms have lasted for more than 3 days, we recommend you book an appointment to see a GP."
            
        else:
            symptom_result = "self_care"
            response_text = "Your symptoms appear to be mild. We recommend self-care measures such as rest, hydration, and over-the-counter remedies."
        
        # If a doctor is needed, find and list them.
        available_doctors = []
        if symptom_result in ["gp", "specialist"]:
            available_doctors = get_available_doctors(symptom_result)

            if available_doctors:
                doctor_list_text = "\n\nAvailable doctors and their specialties:\n"
                for i, doctor in enumerate(available_doctors):
                    # Format the time slot for display
                    appointment_time = doctor.get('time_slot')
                    
                    if isinstance(appointment_time, datetime):
                        formatted_date = appointment_time.strftime("%A, %B %d, %Y")
                        formatted_time = appointment_time.strftime("%I:%M %p")
                    else: # Handle Firestore Timestamp object
                        formatted_date = appointment_time.strftime("%A, %B %d, %Y")
                        formatted_time = appointment_time.strftime("%I:%M %p")
                        
                    doctor_list_text += f"{i+1}. Dr. {doctor.get('name')}\n"
                    doctor_list_text += f"    - Date & Time: {formatted_date} at {formatted_time}\n"
                    doctor_list_text += f"    - Clinic: {doctor.get('clinic_address')}\n"
                
                response_text += doctor_list_text + "\nWould you like to check if your insurance covers your visit with any of these doctors?"
            else:
                response_text += " There are no available doctors at this time. Please try again later."
        
        # This webhook can also set parameters to guide the Dialogflow flow.
        response_params = {
            "symptom_result": symptom_result,
            "doctor_available": bool(available_doctors),
            # Store the full doctor objects in the session to avoid re-querying the database.
            "doctor_info_list": available_doctors
        }

        # Construct the final JSON response to be sent back to Dialogflow.
        response = {
            "sessionInfo": {"parameters": response_params},
            "fulfillmentResponse": {"messages": [{"text": {"text": [response_text]}}]}
        }
        
        return jsonify(response)

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return jsonify({
            "fulfillmentResponse": {
                "messages": [{"text": {"text": ["An error occurred while processing your request. Please try again later."]}}]
            }
        }), 500

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get('PORT', 8000)))
