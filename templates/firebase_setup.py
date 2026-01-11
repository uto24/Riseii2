import os
import json
import firebase_admin
from firebase_admin import credentials, firestore

def initialize_firebase():
    # On Render, we paste the entire Service Account JSON into an ENV variable
    # named FIREBASE_CREDENTIALS_JSON
    cred_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')

    if cred_json:
        cred_dict = json.loads(cred_json)
        cred = credentials.Certificate(cred_dict)
    else:
        # Fallback for local dev if you have the file
        cred = credentials.Certificate("serviceAccountKey.json")

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)

    return firestore.client()

db = initialize_firebase()
