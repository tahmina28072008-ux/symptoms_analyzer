# Import the necessary libraries for a Flask web server and JSON handling.
import json
from flask import Flask, request, jsonify

# Initialize the Flask application.
app = Flask(__name__)

# Define the root path for our webhook.
@app.route('/', methods=['POST'])
def webhook():
    """
    This function handles the incoming webhook request from Dialogflow CX.
    It processes the user's symptoms and returns a result.
    """
    try:
        # Get the JSON data from the Dialogflow CX request.
        req = request.get_json(silent=True, force=True)
        
        # Log the full request for debugging purposes.
        print("Webhook Request:")
        print(json.dumps(req, indent=2))

        # We'll assume the symptom information is in a session parameter
        # or captured from a form on the previous page. For this example,
        # we'll look for parameters named 'symptoms_list' and 'symptom_duration_days'.
        # Replace these with the actual parameter names from your form.
        
        # Safely get the session parameters from the request.
        session_params = req.get('sessionInfo', {}).get('parameters', {})
        symptoms_list = session_params.get('symptoms_list', [])
        symptom_duration_days = session_params.get('symptom_duration_days', 0)

        # Simple logic to determine the result based on the symptoms.
        # This is where you would integrate a more complex system (e.g., a database
        # lookup or a machine learning model) to analyze the symptoms.
        symptom_result = "self_care"
        
        # A simple check for emergency keywords.
        symptom_text = ' '.join(symptoms_list).lower()
        if "emergency" in symptom_text or "unconscious" in symptom_text or "severe breathing" in symptom_text:
            symptom_result = "emergency"
        # Check for prolonged symptoms.
        elif symptom_duration_days >= 14: # 2 weeks = 14 days
            symptom_result = "specialist"
        elif symptom_duration_days >= 3:
            symptom_result = "gp"
        # Default case for general health issues.
        else:
            symptom_result = "self_care"

        # Prepare the webhook response.
        # The 'sessionInfo' block is used to set the session parameter.
        # 'parameters' is the key that holds all the session parameters.
        # 'symptom_result' is the new parameter we are setting.
        response = {
            "sessionInfo": {
                "parameters": {
                    "symptom_result": symptom_result
                }
            },
            "fulfillmentResponse": {
                "messages": [
                    {
                        "text": {
                            "text": [f"Analyzing your symptoms... Result is: {symptom_result}"]
                        }
                    }
                ]
            }
        }

        # Return the JSON response.
        return jsonify(response)

    except Exception as e:
        # Catch any errors and return a helpful error message for debugging.
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

# This is for local development. When deployed, the web server
# (like Gunicorn or uWSGI) will handle running the app.
if __name__ == '__main__':
    app.run(debug=True, port=8000)
