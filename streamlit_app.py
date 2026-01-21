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

ns = {'ans': 'http://example.org/assessment'}


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
def get_auditor_response(user_input, csf_data):
    """
    csf_data is a dict containing:
    'id', 'name', 'type', 'multiplier', 'criteria'
    """
    api_key = st.secrets.get("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    # Contextual prompt construction based on XML attributes
    criteria_str = "\n".join([f"- {c}" for c in csf_data['criteria']])
    
    sys_instr = f"""
    ROLE: SPL Lead Auditor (Corporate/Procurement Focus).
    CSF CONTEXT: {csf_data['name']} (ID: {csf_data['id']}).
    MODE: {csf_data['type']} Evaluation.
    
    CRITERIA STANDARDS:
    {criteria_str}
    
    INSTRUCTIONS:
    1. Conduct a professional handshake. Ask the user if they believe they currently meet the criteria regarding this particular CSF. For instance, if it is a document or policy, does it meet the requirements shown?
    2. If MODE is 'Binary': Decide if the response indicates a total 'SUCCESS'. If so, append [VALIDATE: ALL].
    3. If MODE is 'Proportional': Evaluate 'fitness for purpose' (0.0 to 1.0). Append [SCORE: 0.X] based on your best guess of readiness.
    4. For individual criteria met, append [ITEM_MET: Exact Criteria Text].
    5. Maintain a professional, forensic tone.
    """
    
    # Maintain session context for the specific CSF
    chat = model.start_chat(history=st.session_state.chat_history)
    response = chat.send_message(f"{sys_instr}\n\nUser Evidence: {user_input}")
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

def calculate_live_score(root, archived_status):
    total_weighted = 0
    
    for category in root.findall('ans:Category', ns):
        for csf in category.findall('ans:CSF', ns):
            csf_id = csf.get('id')
            multiplier = float(csf.find('.//ans:Multiplier', ns).text)
            
            # Get all criteria for this factor
            criteria_items = [i.text for i in csf.findall(".//ans:Item", ns)]
            if not criteria_items:
                continue
                
            # Count validated items for this CSF
            user_met_dict = archived_status.get(csf_id, {})
            met_count = sum(1 for item in criteria_items if user_met_dict.get(item))
            
            # Apply weighted logic: (Completed / Total) * Multiplier
            total_weighted += (met_count / len(criteria_items)) * multiplier
                
    return int(total_weighted)


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
            # UPDATED: Use the 'fields' parameter to relabel the Username box to Email
            auth_result = authenticator.login(
                location="main", 
                key="spl_login_form",
                fields={'Form name': 'Login', 'Username': 'Corporate Email', 'Password': 'Password'}
    )
            
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
        live_score = calculate_live_score(root, st.session_state.archived_status)
        st.metric("WEIGHTED SCORE", f"{live_score} PTS")
        
        st.divider()
        st.subheader("📁 Categories")
        for cat in root.findall('Category'):
            if st.button(cat.get('name')):
                st.session_state.active_cat = cat.get('id')

    # MAIN INTERFACE: 3 Columns
    col1, col2, col3 = st.columns([0.2, 0.5, 0.3], gap="medium")

    # --- COLUMN 1: CSF SELECTION WITH TICK LOGIC ---
    with col1:
        st.subheader("Critical Success Factors")
        
        # Ensure we have a default category if none is selected
        active_cat_id = st.session_state.get("active_cat", "CAT-GOV")
        
        # SURGICAL FIX: Use global ns to find the category node
        category_node = root.find(f".//ans:Category[@id='{active_cat_id}']", ns)
        
        if category_node is not None:
            # Loop through CSF children using the global namespace
            for csf in category_node.findall('ans:CSF', ns):
                csf_id = csf.get('id')
                csf_name = csf.get('name')
                
                # Logic to check if this CSF is "Complete" (All MUSTs met)
                # Use ans:Item and ns to find the items within the CSF
                must_items = [i.text for i in csf.findall(".//ans:Item[@priority='Must']", ns)]
                met_items = st.session_state.archived_status.get(csf_id, {})
                
                # Check if every 'Must' item is True in our ledger
                is_complete = all(met_items.get(item) for item in must_items) if must_items else False
                
                # Append tick icon to label if all must-haves are validated
                display_label = f"{csf_name} ✅" if is_complete else csf_name
                
                # Render the high-contrast button
                is_active = st.session_state.active_csf == csf_id
                if st.button(
                    display_label, 
                    key=f"btn_v2_{csf_id}", # New key to avoid potential cache collision
                    type="primary" if is_active else "secondary",
                    use_container_width=True
                ):
                    st.session_state.active_csf = csf_id
                    st.session_state.chat_history = []
                    st.rerun()
        else:
            st.info("Select a Category in the sidebar to initialize factors.")

    # COLUMN 2: The Validation Chat
    with col2:
        active_csf_node = root.find(f".//CSF[@id='{st.session_state.active_csf}']")
        st.subheader(f"💬 Validating: {active_csf_node.get('name')}")
        
        chat_container = st.container(height=500)
        for msg in st.session_state.chat_history:
            with chat_container.chat_message(msg["role"]):
                st.write(msg["content"])
                
        if user_input := st.chat_input("Provide evidence..."):
            # --- 1. DEFINE CSF_CONTEXT FIRST ---
            # This pulls the metadata from the currently selected XML node
            active_csf_node = root.find(f".//ans:CSF[@id='{st.session_state.active_csf}']", ns)
            
            csf_context = {
                'id': st.session_state.active_csf,
                'name': active_csf_node.get('name'),
                'type': active_csf_node.find('ans:Type', ns).text, # Binary or Proportional
                'multiplier': active_csf_node.find('ans:Multiplier', ns).text,
                'criteria': [i.text for i in active_csf_node.findall('.//ans:Item', ns)]
        }
            
            # 1. Store user message
            st.session_state.chat_history.append({"role": "user", "content": user_input})
            
            # 2. Get AI Response (Ensure csf_context is passed as defined previously)
            response = get_auditor_response(user_input, csf_context)
            
            # --- START INGESTION LOGIC ---
            # A. Handle Binary Pass
            if "[VALIDATE: ALL]" in response:
                for item in csf_context['criteria']:
                    if st.session_state.active_csf not in st.session_state.archived_status:
                        st.session_state.archived_status[st.session_state.active_csf] = {}
                    st.session_state.archived_status[st.session_state.active_csf][item] = True
                st.toast("✅ CSF FULLY VALIDATED")

            # B. Handle Proportional Guess
            import re
            score_match = re.search(r"\[SCORE: (\d+\.\d+)\]", response)
            if score_match:
                current_score = float(score_match.group(1))
                if "csf_scores" not in st.session_state:
                    st.session_state.csf_scores = {}
                st.session_state.csf_scores[st.session_state.active_csf] = current_score
                st.toast(f"📊 Readiness Updated: {int(current_score * 100)}%")

            # C. Handle individual item triggers
            for item in csf_context['criteria']:
                if f"[ITEM_MET: {item}]" in response:
                    if st.session_state.active_csf not in st.session_state.archived_status:
                        st.session_state.archived_status[st.session_state.active_csf] = {}
                    st.session_state.archived_status[st.session_state.active_csf][item] = True
            # --- END INGESTION LOGIC ---

            # 3. Clean and store assistant message
            clean_resp = re.sub(r"\[.*?\]", "", response).strip()
            st.session_state.chat_history.append({"role": "assistant", "content": clean_resp})
            
            # 4. Trigger rerun to update Checklist (Col 3) and Speedometer (Sidebar)
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