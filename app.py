
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
# --- UPDATED LOGIN DECORATOR (AUTO LOGOUT BANNED USER) ---
from functools import wraps # এটি ইম্পোর্ট করা ভালো (ফাইলের উপরে ইম্পোর্ট সেকশনে না থাকলে সমস্যা নেই, তবে রাখা ভালো)

def login_required(f):
    @wraps(f) # এটি ফাংশনের নাম ঠিক রাখে
    def wrapper(*args, **kwargs):
        # ১. সেশন চেক
        if 'user_id' not in session:
            return redirect(url_for('auth'))
        
        # ২. ডাটাবেস চেক (ব্যান কিনা দেখার জন্য)
        try:
            uid = session['user_id']
            # শুধুমাত্র 'is_banned' ফিল্ডটি চেক করার জন্য হালকা কুয়েরি
            user_doc = db.collection('users').document(uid).get(['is_banned'])
            
            if user_doc.exists:
                user_data = user_doc.to_dict()
                
                # ⛔ যদি ইউজার BANNED হয়
                if user_data.get('is_banned', False):
                    session.clear() # সেশন ডিলিট
                    flash("Your account has been BANNED by Admin.", "error")
                    return redirect(url_for('auth')) # লগইন পেজে পাঠিয়ে দিবে
            else:
                # যদি ডাটাবেসে ইউজার না থাকে (ডিলিট হয়ে যায়)
                session.clear()
                return redirect(url_for('auth'))
                
        except Exception as e:
            print(f"Security Check Error: {e}")
            # এরর হলেও সেফটির জন্য লগআউট করে দেওয়া ভালো, অথবা পাস করা যেতে পারে
            
        return f(*args, **kwargs)
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
    # এখন আর সরাসরি রিডাইরেক্ট করবে না, ল্যান্ডিং পেজ দেখাবে
    return render_template('landing.html')
@app.route('/auth', methods=['GET', 'POST'])
def auth():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('auth.html', config=firebase_config)

@app.route('/session_login', methods=['POST'])
def session_login():
    data = request.json
    id_token = data.get('idToken')
    ref_code = data.get('refCode')
    
    # ফ্রন্টএন্ড থেকে নাম ও ফেসবুক লিংক আসছে
    name = data.get('name')
    fb_link = data.get('fb_link')
    
    try:
        # ১. টোকেন ভেরিফাই করা
        decoded_token = admin_auth.verify_id_token(id_token)
        uid = decoded_token['uid']
        email = decoded_token['email']
        
        user_ref = db.collection('users').document(uid)
        user_doc = user_ref.get()
        
        # --- [STEP 1] BAN CHECK (যদি ইউজার আগে থেকেই থাকে) ---
        if user_doc.exists:
            user_data = user_doc.to_dict()
            if user_data.get('is_banned', False):
                return jsonify({"status": "error", "message": "Your account has been BANNED by Admin."}), 403

        # --- [STEP 2] NEW USER REGISTRATION (যদি ইউজার না থাকে) ---
        if not user_doc.exists:
            # নাম না থাকলে ইমেইল থেকে নাম বানানো
            if not name: 
                name = email.split('@')[0]

            initial_balance = 0.0
            referred_by_uid = None

            # --- REFERRAL LOGIC ---
            if ref_code and ref_code != uid:
                referrer_ref = db.collection('users').document(ref_code)
                referrer_doc = referrer_ref.get()
                
                if referrer_doc.exists:
                    referred_by_uid = ref_code
                    
                    # A. নতুন ইউজারকে বোনাস (৫ টাকা)
                    initial_balance = 10.0 
                    
                    # B. যে রেফার করেছে তাকে বোনাস (৫ টাকা)
                    referrer_data = referrer_doc.to_dict()
                    new_ref_balance = referrer_data.get('balance', 0) + 10.0
                    new_ref_count = referrer_data.get('referral_count', 0) + 1
                    
                    referrer_ref.update({
                        'balance': new_ref_balance,
                        'referral_count': new_ref_count
                    })
                    
                    # History Log (Referrer)
                    db.collection('balance_history').add({
                        'uid': ref_code,
                        'type': 'referral_bonus',
                        'amount': 5.0,
                        'description': f'Referral Bonus: {name}',
                        'timestamp': datetime.datetime.now()
                    })

                    # History Log (New User)
                    db.collection('balance_history').add({
                        'uid': uid,
                        'type': 'signup_bonus',
                        'amount': 5.0,
                        'description': 'Welcome Bonus',
                        'timestamp': datetime.datetime.now()
                    })
            # --- END REFERRAL LOGIC ---

            # নতুন ইউজার সেভ করা
            new_user_data = {
                'email': email,
                'name': name,
                'fb_link': fb_link,
                'balance': initial_balance,
                'role': 'user',
                'is_banned': False, # ডিফল্ট ভাবে ব্যান ফলস থাকবে
                'is_active': False, # একাউন্ট ফি না দেওয়া পর্যন্ত ইনঅ্যাক্টিভ
                'created_at': datetime.datetime.now(),
                'referral_count': 0,
                'referred_by': referred_by_uid
            }
            user_ref.set(new_user_data)
            is_admin = False
            
        else:
            # যদি ইউজার থাকে, তার রোল চেক করা
            is_admin = user_doc.to_dict().get('role') == 'admin'

        # --- [STEP 3] CREATE SESSION ---
        session.permanent = True
        session['user_id'] = uid
        session['email'] = email
        session['is_admin'] = is_admin
        
        return jsonify({"status": "success"})

    except Exception as e:
        print(f"Login Error: {e}")
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
    
    # ১. ইউজারের তথ্য আনা
    user_doc = db.collection('users').document(uid).get()
    if not user_doc.exists:
        session.clear()
        return redirect(url_for('auth'))
    user = user_doc.to_dict()
    
    # ২. ব্যালেন্স হিস্টোরি আনা (সর্বশেষ ১০টি)
    balance_history = db.collection('balance_history')\
        .where(field_path='uid', op_string='==', value=uid)\
        .order_by('timestamp', direction=Query.DESCENDING).limit(10).stream()
    history = [h.to_dict() for h in balance_history]

    # ৩. রেফারেল লিস্ট আনা
    referrals_stream = db.collection('users')\
        .where(field_path='referred_by', op_string='==', value=uid).stream()
    referrals = [{'email': r.to_dict().get('email'), 'name': r.to_dict().get('name', 'Unknown'), 'joined': r.to_dict().get('created_at')} for r in referrals_stream]

    # ৪. টাস্ক পরিসংখ্যান (Stats) বের করা
    # দ্রষ্টব্য: বড় অ্যাপের জন্য এটি ডাটাবেসে কাউন্টার হিসেবে রাখা ভালো, তবে এখন রিয়েলটাইম কাউন্ট করছি
    all_tasks = db.collection('task_submissions').where(field_path='uid', op_string='==', value=uid).stream()
    
    stats = {
        'approved': 0,
        'pending': 0,
        'rejected': 0,
        'total_earned_from_tasks': 0
    }

    # লুপ চালিয়ে কাউন্ট করা
    for t in all_tasks:
        data = t.to_dict()
        status = data.get('status')
        if status == 'approved':
            stats['approved'] += 1
        elif status == 'pending':
            stats['pending'] += 1
        elif status == 'rejected':
            stats['rejected'] += 1

    return render_template('dashboard.html', 
                           user=user, 
                           history=history, 
                           referrals=referrals, 
                           stats=stats,
                           uid=uid)



# --- 1. NEW ROUTE FOR USER MANAGEMENT (Add this block) ---
@app.route(f'/{ADMIN_ROUTE}/ui')
@admin_required
def manage_users():
    # Pagination Logic
    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    # Fetch Users
    users_stream = db.collection('users')\
        .order_by('created_at', direction=Query.DESCENDING)\
        .offset(offset)\
        .limit(per_page)\
        .stream()
        
    users_list = [{'id': d.id, **d.to_dict()} for d in users_stream]
    
    # Simple pagination check
    has_next = len(users_list) == per_page
    has_prev = page > 1

    return render_template('manage_users.html', 
                           users=users_list,
                           page=page,
                           has_next=has_next,
                           has_prev=has_prev,
                           admin_route=ADMIN_ROUTE)

# --- 2. UPDATE ACTION ROUTES (Redirect to new page) ---

@app.route(f'/{ADMIN_ROUTE}/ban_user/<uid>')
@admin_required
def ban_user(uid):
    db.collection('users').document(uid).update({'is_banned': True})
    flash("User BANNED.", "success")
    # Redirect back to user list
    return redirect(f'/{ADMIN_ROUTE}/users')

@app.route(f'/{ADMIN_ROUTE}/unban_user/<uid>')
@admin_required
def unban_user(uid):
    db.collection('users').document(uid).update({'is_banned': False})
    flash("User UNBANNED.", "success")
    return redirect(f'/{ADMIN_ROUTE}/users')

@app.route(f'/{ADMIN_ROUTE}/delete_user/<uid>')
@admin_required
def delete_user(uid):
    db.collection('users').document(uid).delete()
    flash("User DELETED.", "success")
    return redirect(f'/{ADMIN_ROUTE}/users')

# --- 3. CLEAN UP ADMIN PANEL (Remove old user fetching) ---
# আপনার admin_panel ফাংশনে 'users_list' এর অংশটুকু মুছে দিন বা নিচের মতো আপডেট করুন:

@app.route('/tasks', methods=['GET', 'POST'])
@login_required
def tasks():
    uid = session['user_id']
    
    # --- TASK SUBMISSION LOGIC ---
    if request.method == 'POST':
        task_id = request.form.get('task_id')
        
        # Security: Check duplicate submission
        existing_sub = db.collection('task_submissions').where(
            field_path='uid', op_string='==', value=uid
        ).where(
            field_path='task_id', op_string='==', value=task_id
        ).get()
            
        if existing_sub:
            flash("You have already submitted this task!", "error")
            return redirect(url_for('tasks'))

        task_type = request.form.get('task_type')
        submission_data = {
            'uid': uid,
            'task_id': task_id,
            'status': 'pending',
            'timestamp': datetime.datetime.now(),
            'email': session['email']
        }
        
        # Image Upload
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

    # --- GET TASKS LIST (FILTERED) ---
    
    # ১. সব টাস্ক আনো
    tasks_ref = db.collection('tasks').stream()
    
    # ২. ইউজার যা সাবমিট করেছে তা আনো
    user_submissions = db.collection('task_submissions').where(
        field_path='uid', op_string='==', value=uid
    ).stream()
    
    # সাবমিট করা টাস্কের আইডি লিস্ট
    submitted_task_ids = [sub.to_dict().get('task_id') for sub in user_submissions]
    
    tasks_list = []
    for t in tasks_ref:
        # ৩. ফিল্টার লজিক: যদি টাস্কটি অলরেডি করা থাকে, তবে বাদ দাও (continue)
        if t.id in submitted_task_ids:
            continue 
            
        # যদি করা না থাকে, তবেই লিস্টে যোগ করো
        task_data = t.to_dict()
        task_data['id'] = t.id
        tasks_list.append(task_data)
    
    # Sort manually (New tasks first)
    tasks_list.sort(key=lambda x: str(x.get('created_at', '')), reverse=True)
    
    return render_template('tasks.html', tasks=tasks_list)
@app.route('/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    uid = session['user_id']
    user_ref = db.collection('users').document(uid)
    user = user_ref.get().to_dict()
    
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount'))
            method = request.form.get('method')
            number = request.form.get('number')
            
            # --- 1. MINIMUM & BALANCE CHECK ---
            if amount < 50: 
                flash("Minimum withdraw amount is 50 BDT.", "error")
                return redirect(url_for('withdraw'))
            
            if user.get('balance', 0) < amount:
                flash("Insufficient wallet balance.", "error")
                return redirect(url_for('withdraw'))

            # --- 2. ELIGIBILITY CHECK (250 TK + 3 REF) ---
            # উইথড্র বাটনে চাপার পর চেক হবে
            if user.get('balance', 0) < 250 or user.get('referral_count', 0) < 3:
                flash(f"Withdraw requires 250 BDT & 3 Referrals. (You have: {user.get('balance')} BDT, {user.get('referral_count')} Ref)", "error")
                return redirect(url_for('withdraw'))

            # --- 3. ACTIVATION CHECK ---
            # শর্ত পূরণ হয়েছে, কিন্তু একাউন্ট অ্যাক্টিভ নেই? তাহলে অ্যাক্টিভেশন পেজে পাঠাও
            if not user.get('is_active', False): 
                flash("Conditions met! Please activate your account first.", "success")
                return render_template('activation.html')

            # --- 4. SUCCESS: PROCESS WITHDRAW ---
            # সব শর্ত ঠিক থাকলে এবং একাউন্ট অ্যাক্টিভ থাকলে
            db.collection('withdraw_requests').add({
                'uid': uid,
                'email': session['email'],
                'amount': amount,
                'method': method,
                'number': number,
                'status': 'pending',
                'timestamp': datetime.datetime.now()
            })
            
            # ব্যালেন্স কাটা
            user_ref.update({'balance': user['balance'] - amount})
            
            # হিস্টোরি লগ
            db.collection('balance_history').add({
                'uid': uid,
                'type': 'withdraw_hold',
                'amount': -amount,
                'timestamp': datetime.datetime.now()
            })
            
            flash("Withdraw request sent successfully!", "success")
            return redirect(url_for('withdraw'))

        except ValueError:
            flash("Invalid amount entered.", "error")
            
    # GET Request: সবসময় ফর্ম দেখাবে
    return render_template('withdraw.html', user=user)
# --- NEW ROUTE: ACTIVATION SUBMISSION ---
@app.route('/submit_activation', methods=['POST'])
@login_required
def submit_activation():
    uid = session['user_id']
    
    # ডাটাবেসে রিকোয়েস্ট জমা রাখা (Admin Panel এ দেখানোর জন্য)
    db.collection('activation_requests').add({
        'uid': uid,
        'email': session['email'],
        'method': request.form.get('method'),
        'sender_number': request.form.get('sender_number'),
        'trx_id': request.form.get('trx_id'),
        'status': 'pending',
        'timestamp': datetime.datetime.now()
    })
    
    flash("অ্যাক্টিভেশন রিকোয়েস্ট জমা হয়েছে! অ্যাডমিন অ্যাপ্রুভ করলে আপনি উইথড্র করতে পারবেন।", "success")
    return redirect(url_for('dashboard'))
@app.route(f'/{ADMIN_ROUTE}', methods=['GET', 'POST'])
@admin_required
def admin_panel():
    # --- 1. HANDLE POST REQUESTS ---
    if request.method == 'POST':
        # Create Task
        if 'create_task' in request.form:
            try:
                task_link = request.form.get('task_link')
                if not task_link: task_link = ""

                db.collection('tasks').add({
                    'title': request.form.get('title'),
                    'category': request.form.get('category'),
                    'task_link': task_link,
                    'description': request.form.get('description'),
                    'reward': float(request.form.get('reward')),
                    'proof_requirement': request.form.get('proof_requirement'),
                    'created_at': datetime.datetime.now()
                })
                flash("New Task Published!", "success")
            except Exception as e:
                flash(f"Error: {e}", "error")

        # Update User Balance
        elif 'update_balance' in request.form:
            target_uid = request.form.get('target_uid')
            new_amount = float(request.form.get('amount'))
            action_type = request.form.get('action_type')
            
            user_ref = db.collection('users').document(target_uid)
            user_data = user_ref.get().to_dict()
            if user_data:
                current_bal = user_data.get('balance', 0.0)
                final_bal = (current_bal + new_amount) if action_type == 'add' else (current_bal - new_amount)
                user_ref.update({'balance': final_bal})
                flash("User balance updated.", "success")

    # --- 2tt. DATA FETCHING ---
    
    # Pending Tasks
    p_tasks = db.collection('task_submissions').where(field_path='status', op_string='==', value='pending').stream()
    pending_tasks = [{'id': d.id, **d.to_dict()} for d in p_tasks]

    # Pending Withdraws
    p_withdraws = db.collection('withdraw_requests').where(field_path='status', op_string='==', value='pending').stream()
    pending_withdraws = [{'id': d.id, **d.to_dict()} for d in p_withdraws]

    # Activation Requests
    act_reqs = db.collection('activation_requests').where(field_path='status', op_string='==', value='pending').stream()
    activation_requests = [{'id': d.id, **d.to_dict()} for d in act_reqs]

    # Active Tasks (Limited to 20)
    all_tasks_stream = db.collection('tasks').order_by('created_at', direction=Query.DESCENDING).limit(20).stream()
    active_tasks = [{'id': d.id, **d.to_dict()} for d in all_tasks_stream]

    return render_template('admin.html', 
                           pending_tasks=pending_tasks, 
                           pending_withdraws=pending_withdraws,
                           activation_requests=activation_requests,
                           active_tasks=active_tasks)
    
@app.route(f'/{ADMIN_ROUTE}/approve_activation/<req_id>/<user_uid>')
@admin_required
def approve_activation(req_id, user_uid):
    # 1. User কে Active করা
    db.collection('users').document(user_uid).update({'is_active': True})
    
    # 2. Request status update
    db.collection('activation_requests').document(req_id).update({'status': 'approved'})
    
    flash("User Account Activated Successfully!", "success")
    return redirect(f'/{ADMIN_ROUTE}')
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
    # ১. রিকোয়েস্ট ডাটা আনা
    req_ref = db.collection('withdraw_requests').document(req_id)
    req = req_ref.get().to_dict()
    
    if req and req['status'] == 'pending':
        # ২. স্ট্যাটাস আপডেট করা (Request Table)
        req_ref.update({'status': 'paid'})
        
        # ৩. হিস্টোরি আপডেট করা (যাতে ড্যাশবোর্ডে Hold না দেখায়)
        # ইউজারের ওই নির্দিষ্ট পরিমাণের 'Hold' এন্ট্রিটি খুঁজে বের করা
        history_query = db.collection('balance_history')\
            .where(field_path='uid', op_string='==', value=req['uid'])\
            .where(field_path='amount', op_string='==', value=-req['amount'])\
            .where(field_path='type', op_string='==', value='withdraw_hold')\
            .limit(1).stream()
            
        for doc in history_query:
            # Hold কে Paid এ পরিবর্তন করা
            doc.reference.update({
                'type': 'withdraw_paid',
                'description': f"Paid via {req['method']} ({req['number']})"
            })
            
        flash("Withdraw marked as PAID & History Updated.", "success")
    
    return redirect(f'/{ADMIN_ROUTE}')
@app.route(f'/{ADMIN_ROUTE}/reject_withdraw/<req_id>')
@admin_required
def reject_withdraw(req_id):
    # Refund Balance
    req_ref = db.collection('withdraw_requests').document(req_id)
    req = req_ref.get().to_dict()
    
    if req and req['status'] == 'pending':
        user_ref = db.collection('users').document(req['uid'])
        user_data = user_ref.get().to_dict()
        # Add the money back to user account
        user_ref.update({'balance': user_data['balance'] + req['amount']})
        
        req_ref.update({'status': 'rejected'})
        flash("Withdraw rejected & Refunded.", "success")
        
    return redirect(url_for('admin_panel'))

# Required for Vercel
app = app 

if __name__ == '__main__':
    app.run(debug=True)
