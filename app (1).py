import os
import json
import requests
import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from datetime import timedelta
from firebase_setup import db
from firebase_admin import auth as admin_auth
from google.cloud.firestore import Query

# Load Envs
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# --- CONFIGURATION ---
app.secret_key = os.getenv("SECRET_KEY", "dev_secret")
app.permanent_session_lifetime = timedelta(days=7)
ADMIN_ROUTE = os.getenv("ADMIN_ROUTE", "admin")
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY")

# --- FIREBASE CLIENT CONFIG (Passed to Frontend) ---
firebase_config = {
    "apiKey": os.getenv("FIREBASE_API_KEY"),
    "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN"),
    "projectId": os.getenv("FIREBASE_PROJECT_ID"),
    "storageBucket": os.getenv("FIREBASE_STORAGE_BUCKET"),
    "messagingSenderId": os.getenv("FIREBASE_MESSAGING_SENDER_ID"),
    "appId": os.getenv("FIREBASE_APP_ID")
}

# --- HELPERS ---

def login_required(f):
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def admin_required(f):
    def wrapper(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'):
            flash("Unauthorized access.", "error")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def upload_to_imgbb(image_file):
    try:
        url = "https://api.imgbb.com/1/upload"
        payload = {
            "key": IMGBB_API_KEY,
        }
        files = {
            "image": image_file.read()
        }
        response = requests.post(url, data=payload, files=files)
        data = response.json()
        if data['success']:
            return data['data']['url']
    except Exception as e:
        print(f"Upload Error: {e}")
    return None

# --- ROUTES ---

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('auth'))

@app.route('/auth', methods=['GET', 'POST'])
def auth():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('auth.html', config=firebase_config)

@app.route('/session_login', methods=['POST'])
def session_login():
    """Receives Firebase ID Token from frontend, verifies it, creates Flask Session"""
    data = request.json
    id_token = data.get('idToken')
    
    try:
        decoded_token = admin_auth.verify_id_token(id_token)
        uid = decoded_token['uid']
        email = decoded_token['email']
        
        # Check/Create User in Firestore
        user_ref = db.collection('users').document(uid)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            user_ref.set({
                'email': email,
                'balance': 0.0,
                'role': 'user',
                'created_at': datetime.datetime.now(),
                'referral_count': 0
            })
            is_admin = False
        else:
            is_admin = user_doc.to_dict().get('role') == 'admin'

        # Set Session
        session.permanent = True
        session['user_id'] = uid
        session['email'] = email
        session['is_admin'] = is_admin
        
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 401

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for('auth'))

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session['user_id']
    user = db.collection('users').document(uid).get().to_dict()
    
    # Get recent history
    balance_history = db.collection('balance_history')\
        .where('uid', '==', uid).order_by('timestamp', direction=Query.DESCENDING).limit(5).stream()
    
    history = [h.to_dict() for h in balance_history]
    
    return render_template('dashboard.html', user=user, history=history)

@app.route('/tasks', methods=['GET', 'POST'])
@login_required
def tasks():
    uid = session['user_id']
    
    if request.method == 'POST':
        task_id = request.form.get('task_id')
        task_type = request.form.get('task_type') # 'link', 'image', 'text'
        
        submission_data = {
            'uid': uid,
            'task_id': task_id,
            'status': 'pending',
            'timestamp': datetime.datetime.now(),
            'email': session['email']
        }
        
        # Handle Proof
        if 'image' in request.files and request.files['image'].filename != '':
            img_url = upload_to_imgbb(request.files['image'])
            if not img_url:
                flash("Image upload failed.", "error")
                return redirect(url_for('tasks'))
            submission_data['proof'] = img_url
            submission_data['proof_type'] = 'image'
        else:
            submission_data['proof'] = request.form.get('proof_text')
            submission_data['proof_type'] = 'text/link'
            
        db.collection('task_submissions').add(submission_data)
        flash("Task submitted for review!", "success")
        return redirect(url_for('tasks'))

    # Get Available Tasks
    tasks_ref = db.collection('tasks').stream()
    tasks_list = [{'id': t.id, **t.to_dict()} for t in tasks_ref]
    
    return render_template('tasks.html', tasks=tasks_list)

@app.route('/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    uid = session['user_id']
    user_ref = db.collection('users').document(uid)
    user = user_ref.get().to_dict()
    
    if request.method == 'POST':
        amount = float(request.form.get('amount'))
        method = request.form.get('method')
        number = request.form.get('number')
        
        if amount < 50: # Example min limit
            flash("Minimum withdraw is 50 BDT.", "error")
        elif user['balance'] < amount:
            flash("Insufficient balance.", "error")
        else:
            # Create Request
            db.collection('withdraw_requests').add({
                'uid': uid,
                'email': session['email'],
                'amount': amount,
                'method': method,
                'number': number,
                'status': 'pending',
                'timestamp': datetime.datetime.now()
            })
            # Deduct Balance Immediately (safe hold)
            user_ref.update({'balance': user['balance'] - amount})
            
            # Log Transaction
            db.collection('balance_history').add({
                'uid': uid,
                'type': 'withdraw_hold',
                'amount': -amount,
                'timestamp': datetime.datetime.now()
            })
            
            flash("Withdraw request sent.", "success")
            
    return render_template('withdraw.html', user=user)

# --- ADMIN ROUTES ---
@app.route(f'/{ADMIN_ROUTE}', methods=['GET', 'POST'])
@admin_required
def admin_panel():
    # 1. Handle Task Creation
    if request.method == 'POST' and 'create_task' in request.form:
        db.collection('tasks').add({
            'title': request.form.get('title'),
            'description': request.form.get('description'),
            'reward': float(request.form.get('reward')),
            'task_type': request.form.get('task_type'), # e.g. "YouTube Subscribe"
            'proof_requirement': request.form.get('proof_requirement') # "image" or "link"
        })
        flash("Task created.", "success")
    
    # Data Fetching
    pending_tasks = db.collection('task_submissions').where('status', '==', 'pending').stream()
    pending_withdraws = db.collection('withdraw_requests').where('status', '==', 'pending').stream()
    
    tasks_data = [{'id': d.id, **d.to_dict()} for d in pending_tasks]
    withdraws_data = [{'id': d.id, **d.to_dict()} for d in pending_withdraws]
    
    return render_template('admin.html', 
                           pending_tasks=tasks_data, 
                           pending_withdraws=withdraws_data)

@app.route(f'/{ADMIN_ROUTE}/approve_task/<submission_id>')
@admin_required
def approve_task(submission_id):
    sub_ref = db.collection('task_submissions').document(submission_id)
    sub = sub_ref.get().to_dict()
    
    if sub and sub['status'] == 'pending':
        # Get Task Info for Reward
        task_info = db.collection('tasks').document(sub['task_id']).get().to_dict()
        reward = task_info.get('reward', 0)
        
        # Update User Balance
        user_ref = db.collection('users').document(sub['uid'])
        user_data = user_ref.get().to_dict()
        new_bal = user_data['balance'] + reward
        user_ref.update({'balance': new_bal})
        
        # Update Submission
        sub_ref.update({'status': 'approved'})
        
        # Log
        db.collection('balance_history').add({
            'uid': sub['uid'],
            'type': 'task_earning',
            'amount': reward,
            'description': task_info['title'],
            'timestamp': datetime.datetime.now()
        })
        
        flash("Task Approved & Balance Added.", "success")
    
    return redirect(url_for('admin_panel'))

@app.route(f'/{ADMIN_ROUTE}/reject_task/<submission_id>')
@admin_required
def reject_task(submission_id):
    db.collection('task_submissions').document(submission_id).update({'status': 'rejected'})
    flash("Task Rejected.", "success")
    return redirect(url_for('admin_panel'))

@app.route(f'/{ADMIN_ROUTE}/approve_withdraw/<req_id>')
@admin_required
def approve_withdraw(req_id):
    db.collection('withdraw_requests').document(req_id).update({'status': 'paid'})
    flash("Withdraw marked as PAID.", "success")
    return redirect(url_for('admin_panel'))

@app.route(f'/{ADMIN_ROUTE}/reject_withdraw/<req_id>')
@admin_required
def reject_withdraw(req_id):
    # Refund Balance
    req_ref = db.collection('withdraw_requests').document(req_id)
    req = req_ref.get().to_dict()
    
    if req['status'] == 'pending':
        user_ref = db.collection('users').document(req['uid'])
        user_data = user_ref.get().to_dict()
        user_ref.update({'balance': user_data['balance'] + req['amount']})
        
        req_ref.update({'status': 'rejected'})
        flash("Withdraw rejected & Refunded.", "success")
        
    return redirect(url_for('admin_panel'))

if __name__ == '__main__':
    app.run(debug=True)
