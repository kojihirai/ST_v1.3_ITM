from flask import Flask, render_template, request, jsonify
import os
import shutil
import subprocess
import json
from datetime import datetime
import glob
import requests
import threading
import time
from dotenv import load_dotenv
app = Flask(__name__)

# Update paths to be relative to the lcu directory
UPLOAD_FOLDER = os.getenv('FIRMWARE_UPLOAD_FOLDER')
ARCHIVE_FOLDER = os.getenv('FIRMWARE_ARCHIVE_FOLDER')
MAX_ARCHIVE_VERSIONS = os.getenv('FIRMWARE_MAX_ARCHIVE_VERSIONS')
PM2_APP_NAME = 'firmware-service'  # Updated to match PM2 config

# PagerDuty configuration
PAGERDUTY_ROUTING_KEY = os.getenv('PAGERDUTY_ROUTING_KEY')
PAGERDUTY_EVENTS_URL = os.getenv('PAGERDUTY_EVENTS_URL')

# Flag to prevent status checks during updates
is_updating = False
status_check_thread = None

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ARCHIVE_FOLDER, exist_ok=True)

def send_pagerduty_alert(status):
    if not PAGERDUTY_ROUTING_KEY:
        print("PagerDuty routing key missing. Skipping alert.")
        return

    headers = {
        'Content-Type': 'application/json'
    }

    payload = {
        'payload': {
            'summary': f'Firmware Service Status Alert: {status}',
            'severity': 'critical',
            'source': 'Firmware OTA Service',
            'custom_details': {
                'status': status,
                'service': PM2_APP_NAME,
                'timestamp': datetime.now().isoformat()
            }
        },
        'routing_key': PAGERDUTY_ROUTING_KEY,
        'event_action': 'trigger'
    }

    try:
        response = requests.post(PAGERDUTY_EVENTS_URL, headers=headers, json=payload)
        response.raise_for_status()
        print(f"PagerDuty alert sent successfully: {response.json()}")
    except Exception as e:
        print(f"Failed to send PagerDuty alert: {str(e)}")

def get_pm2_status():
    try:
        result = subprocess.run(['pm2', 'jlist'], capture_output=True, text=True)
        processes = json.loads(result.stdout)
        for process in processes:
            if process['name'] == PM2_APP_NAME:
                status = process['pm2_env']['status']
                # Send PagerDuty alert if status is not 'online'
                if status != 'online':
                    send_pagerduty_alert(status)
                return {
                    'status': status,
                    'uptime': process['pm2_env']['pm_uptime'],
                    'memory': process['monit']['memory'],
                    'cpu': process['monit']['cpu']
                }
        return {'status': 'not_found'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

def archive_current_firmware():
    current_firmware = os.path.join(UPLOAD_FOLDER, 'firmware.py')
    if os.path.exists(current_firmware):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        archive_name = f'firmware_{timestamp}.py'
        shutil.move(current_firmware, os.path.join(ARCHIVE_FOLDER, archive_name))
        
        # Clean up old archives
        archives = glob.glob(os.path.join(ARCHIVE_FOLDER, 'firmware_*.py'))
        archives.sort(reverse=True)
        for old_archive in archives[MAX_ARCHIVE_VERSIONS:]:
            os.remove(old_archive)

def background_status_check():
    while True:
        if not is_updating:
            get_pm2_status()
        time.sleep(60)  # Check every minute

@app.route('/')
def index():
    pm2_status = get_pm2_status()
    return render_template('index.html', pm2_status=pm2_status)

@app.route('/upload', methods=['POST'])
def upload_file():
    global is_updating
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if not file.filename.endswith('.py'):
        return jsonify({'error': 'Only Python files are allowed'}), 400

    try:
        is_updating = True
        archive_current_firmware()
        
        file_path = os.path.join(UPLOAD_FOLDER, 'firmware.py')
        file.save(file_path)
        
        # Restart the firmware service using PM2
        subprocess.run(['pm2', 'restart', PM2_APP_NAME])
        
        is_updating = False
        return jsonify({'message': 'Firmware updated successfully'})
    except Exception as e:
        is_updating = False
        return jsonify({'error': str(e)}), 500

@app.route('/status')
def status():
    return jsonify(get_pm2_status())

if __name__ == '__main__':
    # Start the background status check thread
    status_check_thread = threading.Thread(target=background_status_check, daemon=True)
    status_check_thread.start()
    app.run(host='0.0.0.0', port=1215)
