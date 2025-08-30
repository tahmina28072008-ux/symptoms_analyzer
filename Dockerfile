# Use an official Python runtime as a parent image.
# This provides a pre-configured environment for Python applications.
FROM python:3.10-slim

# Set the working directory to /app inside the container.
# All subsequent commands will be run from this directory.
WORKDIR /app

# Copy the dependencies file into the container at /app.
# This allows Docker to use a cached layer if dependencies don't change.
COPY requirements.txt .

# Install the dependencies from the requirements.txt file.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire local directory (including your webhook code) into the container.
COPY . .

# Expose the port that the Flask application will listen on.
# Cloud Run will use this to route traffic to your service.
EXPOSE 8080

# Define the command to run your application.
# This starts the Gunicorn server, which is a production-ready web server,
# and points it to your Flask application instance. The '-b' flag binds it to all network interfaces.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]
