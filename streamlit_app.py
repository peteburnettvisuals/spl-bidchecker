import streamlit as st
import xml.etree.ElementTree as ET
import google.generativeai as genai
import re
import os
import json
from google.cloud import firestore
from google.oauth2 import service_account
import streamlit_authenticator as stauth
import time

def local_css(file_name):
    with open(file_name) as f:
        st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

local_css("style.css")


# --- 1. TACTICAL SECRET & DB LOADER (MAINTAINED) ---
try:
    credentials_info = st.secrets["gcp_service_account_firestore"]
except (st.errors.StreamlitSecretNotFoundError, KeyError):
    creds_json = os.environ.get("GCP_SERVICE_ACCOUNT_FIRESTORE")
    credentials_info = json.loads(creds_json) if creds_json else None

if not credentials_info:
    st.error("CRITICAL: GCP Credentials missing.")
    st.stop()

gcp_service_creds = service_account.Credentials.from_service_account_info(credentials_info)
db = firestore.Client(credentials=gcp_service_creds, project=credentials_info["project_id"], database="spldb")

# --- 2. ENGINE UTILITIES ---
def load_universal_schema(file_path):
    tree = ET.parse(file_path)
    return tree.getroot()

# --- 3. SESSION STATE INITIALIZATION ---
if "active_csf" not in st.session_state:
    st.session_state.active_csf = "CSF-GOV-01"
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "archived_status" not in st.session_state:
    # Tracks which criteria items are met: { "CSF_ID": { "Criteria_Text": True/False } }
    st.session_state.archived_status = {}

# --- 4. THE AI AUDITOR ENGINE ---
def get_auditor_response(prompt, criteria_list, csf_id):
    api_key = st.secrets.get("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    # System Instruction: AI acts as a Bid Auditor
    sys_instr = f"""
    You are the SPL Bid Auditor. Your goal is to validate if the user meets these criteria: {criteria_list}.
    If evidence is sufficient for a specific item, append [VALIDATE: item_text].
    If the entire CSF is satisfied, append [CSF_ARCHIVE: {csf_id}=SUCCESS].
    Be professional, cynical like a procurement officer, and demand proof. 
    """
    
    chat = model.start_chat(history=[])
    response = chat.send_message(f"{sys_instr}\n\nUser Evidence: {prompt}")
    return response.text

def get_user_credentials():
    creds = {"usernames": {}}
    try:
        users_ref = db.collection("users").stream()
        for doc in users_ref:
            data = doc.to_dict()
            
            # THE FIX: Assign the email field to the 'u_name' variable
            # This tells the login widget to treat the email as the username
            u_name = data.get("email") 
            
            if u_name:
                creds["usernames"][u_name] = {
                    "name": data.get("full_name"),
                    "password": data.get("password"), 
                    "company": data.get("company")
                }
    except Exception as e:
        st.error(f"Intel Sync Error: {e}")
    return creds

# --- AUTHENTICATION GATEKEEPER ---
credentials_data = get_user_credentials()

if "authenticator" not in st.session_state:
    st.session_state.authenticator = stauth.Authenticate(
        credentials_data,
        "spl_bid_cookie",
        "spl_secret_key",
        cookie_expiry_days=30
    )

authenticator = st.session_state.authenticator


# --- 5. UI LAYOUT (3-COLUMN SKETCH) ---
st.set_page_config(layout="wide", page_title="SPL Bid Readiness")

# Load XML data
root = load_universal_schema('bidcheck-config.xml')

# --- THE MAIN UI WRAPPER ---
if not st.session_state.get("authentication_status"):
    # Render the Login UI
    col_l, col_r = st.columns([1, 1], gap="large")
    with col_l:
        st.image("https://peteburnettvisuals.com/wp-content/uploads/2026/01/bidsys-title.png")
        st.markdown("### SYSTEM ACCESS: BID READINESS C2")
        st.info("Public buyers will often exclude bidders at SQ stage if basics are not watertight. ")
    
    with col_r: # This is the right-hand column from your login screen
        st.header("System Access")
        tab_register, tab_login = st.tabs(["Register New Account", "Resume Assessment"])
        
        with tab_login:
            # 1. Standard login widget
            auth_result = authenticator.login(location="main", key="spl_login_form")
            
            # 2. THE SILENT GATE: Catch the login the moment it happens
            if st.session_state.get("authentication_status"):
                # Inject credentials into session state for global access
                st.session_state["username"] = st.session_state["username"]
                st.session_state["name"] = st.session_state["name"]
                
                st.toast(f"Authentication Successful. Accessing SPL C2...")
                time.sleep(0.5) 
                st.rerun() # This triggers the 'else' block immediately
                
            elif st.session_state.get("authentication_status") is False:
                st.error("Invalid Credentials. Check your corporate email and password.")

        with tab_register:
            st.subheader("New User Registration")
            with st.form("registration_form"):
                new_email = st.text_input("Corporate/Work Email")
                new_company = st.text_input("Company Name")
                new_name = st.text_input("Full Name")
                new_password = st.text_input("Password", type="password")
                
                submit_reg = st.form_submit_button("Register")
                
                if submit_reg:
                    if new_email and new_password and new_company:
                        # UPDATED SYNTAX: No more .generate()[0]
                        hashed_password = stauth.Hasher.hash(new_password)
                        
                        # Save to Firestore using email as the unique Document ID
                        db.collection("users").document(new_email).set({
                            "email": new_email,
                            "company": new_company,
                            "full_name": new_name,
                            "password": hashed_password,
                            "created_at": firestore.SERVER_TIMESTAMP,
                        })
                        
                        st.success(f"Company {new_company} registered. Use your email to login.")
                        time.sleep(2)
                        st.rerun()
                    else:
                        st.warning("Please fill in all mandatory fields.")

else:

    # Render the Assess UI    

    # SIDEBAR: The Speedometer & Nav
    with st.sidebar:
        st.header("🦅 TOTAL READINESS")
        # Simple score math for the demo
        total_score = sum([1 for csf in st.session_state.archived_status if any(st.session_state.archived_status[csf].values())])
        st.metric("WEIGHTED SCORE", f"{total_score * 100}") # Placeholder for multiplier logic
        
        st.divider()
        st.subheader("📁 Categories")
        for cat in root.findall('Category'):
            if st.button(cat.get('name')):
                st.session_state.active_cat = cat.get('id')

    # MAIN INTERFACE: 3 Columns
    col1, col2, col3 = st.columns([0.2, 0.5, 0.3], gap="medium")

    # COLUMN 1: CSF Selection
    with col1:
        st.subheader("Critical Success Factors")
        active_cat_id = st.session_state.get("active_cat", "CAT-GOV")
        category_node = root.find(f".//Category[@id='{active_cat_id}']")
        
        for csf in category_node.findall('CSF'):
            is_active = st.session_state.active_csf == csf.get('id')
            if st.button(csf.get('name'), key=csf.get('id'), type="primary" if is_active else "secondary"):
                st.session_state.active_csf = csf.get('id')
                st.session_state.chat_history = [] # Reset chat for new context

    # COLUMN 2: The Validation Chat
    with col2:
        active_csf_node = root.find(f".//CSF[@id='{st.session_state.active_csf}']")
        st.subheader(f"💬 Validating: {active_csf_node.get('name')}")
        
        chat_container = st.container(height=500)
        for msg in st.session_state.chat_history:
            with chat_container.chat_message(msg["role"]):
                st.write(msg["content"])
                
        if user_input := st.chat_input("Provide evidence..."):
            st.session_state.chat_history.append({"role": "user", "content": user_input})
            
            # Get criteria for the AI
            criteria_nodes = active_csf_node.findall(".//Item")
            criteria_texts = [item.text for item in criteria_nodes]
            
            response = get_auditor_response(user_input, criteria_texts, st.session_state.active_csf)
            
            # Parse for [VALIDATE: ...] tags
            for item in criteria_texts:
                if f"[VALIDATE: {item}]" in response:
                    if st.session_state.active_csf not in st.session_state.archived_status:
                        st.session_state.archived_status[st.session_state.active_csf] = {}
                    st.session_state.archived_status[st.session_state.active_csf][item] = True
                    st.toast(f"✅ Criteria Met: {item[:20]}...")

            clean_resp = re.sub(r"\[.*?\]", "", response)
            st.session_state.chat_history.append({"role": "assistant", "content": clean_resp})
            st.rerun()

    # COLUMN 3: MoSCoW Status Boxes
    with col3:
        st.subheader("Requirement Checklist")
        criteria_nodes = active_csf_node.findall(".//Item")
        
        for item_node in criteria_nodes:
            text = item_node.text
            priority = item_node.get("priority")
            is_met = st.session_state.archived_status.get(st.session_state.active_csf, {}).get(text, False)
            
            # Color coding based on your sketch
            bg_color = "#28a745" if is_met else ("#dc3545" if priority == "Must" else "#ffc107")
            st.markdown(f"""
                <div style="background-color:{bg_color}; padding:15px; border-radius:5px; margin-bottom:10px; color:white; font-weight:bold;">
                    [{priority.upper()}] {text}
                </div>
            """, unsafe_allow_html=True)