import os
import json
from flask import Flask, request, jsonify

app = Flask(__name__)

# This is the main webhook endpoint for Dialogflow CX.
@app.route('/', methods=['POST'])
def webhook():
    """
    Handles incoming webhook requests from Dialogflow CX.
    It analyzes the provided symptoms and duration to determine a course of action.
    """
    request_data = request.get_json(silent=True)
    
    # Log the incoming request for debugging purposes.
    # print(json.dumps(request_data, indent=2))

    # Safely extract parameters from the request.
    try:
        # Get session parameters.
        parameters = request_data['sessionInfo']['parameters']
        
        # Extract the symptom duration and symptom list.
        # The duration is expected to be a number.
        duration_days = int(parameters.get('symptom_duration_days', 0))
        # The symptoms list is a string and should be converted to lowercase for case-insensitive matching.
        symptom_text = parameters.get('symptoms_list', '').lower()

    except (KeyError, ValueError) as e:
        # Handle cases where parameters are missing or malformed.
        print(f"Error parsing parameters: {e}")
        response = {
            'fulfillment_response': {
                'messages': [
                    {
                        'text': {
                            'text': [
                                'I am sorry, but I am unable to process your request at this time. Please try again later.'
                            ]
                        }
                    }
                ]
            }
        }
        return jsonify(response)

    # --- Core Logic for Determining Symptom Result and Response Text ---
    # Define a default result for unknown cases.
    symptom_result = "self_care"
    response_text = "For symptoms lasting less than 3 days, we recommend self-care. It's often helpful to rest, stay hydrated by drinking plenty of water, and consider using over-the-counter remedies if needed. Keep an eye on your symptoms, and if they persist or get worse after 3 days, please see a GP."

    # Define keywords for emergency symptoms.
    emergency_keywords = ["chest pain", "difficulty breathing", "severe bleeding"]

    # First, check for emergency conditions. This is the highest priority.
    is_emergency = any(keyword in symptom_text for keyword in emergency_keywords)

    if is_emergency:
        symptom_result = "emergency"
        response_text = "Your symptoms may be a medical emergency. Please seek immediate medical attention. Go to your nearest emergency service or A&E at 123 Baker Street, London, UK."
    # The order of the following 'elif' statements is crucial.
    # Check for the longest duration first (specialist), then the shorter duration (GP).
    elif duration_days >= 14:
        # If symptoms have lasted for 2 weeks or more, refer to a specialist.
        symptom_result = "specialist"
        response_text = "We recommend you book an appointment to see a specialist as your symptoms have been persistent for more than 14 days."
    elif duration_days >= 3:
        # If symptoms have lasted for more than 3 days but less than 2 weeks, refer to a GP.
        symptom_result = "gp"
        response_text = "We recommend you book an appointment to see a GP as your symptoms have been persistent for more than 3 days."
    else:
        # For symptoms lasting less than 3 days, recommend self-care.
        symptom_result = "self_care"
        response_text = "For symptoms lasting less than 3 days, we recommend self-care. It's often helpful to rest, stay hydrated by drinking plenty of water, and consider using over-the-counter remedies if needed. Keep an eye on your symptoms, and if they persist or get worse after 3 days, please see a GP."

    # --- Construct the JSON response for Dialogflow CX ---
    response = {
        'sessionInfo': {
            'parameters': {
                'symptom_result': symptom_result
            }
        },
        'fulfillment_response': {
            'messages': [
                {
                    'text': {
                        'text': [
                            response_text
                        ]
                    }
                }
            ]
        }
    }
    
    # Return the response as JSON.
    return jsonify(response)

# The following is a simple run block for local testing.
if __name__ == '__main__':
    # Get port from environment variable, or use a default.
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
