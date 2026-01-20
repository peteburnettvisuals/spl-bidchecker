import streamlit as st
import xml.etree.ElementTree as ET
import google.generativeai as genai
import re
import datetime
import folium
from streamlit_folium import st_folium
from google.cloud import firestore
from google.oauth2 import service_account
import google.auth
from google.auth.transport.requests import Request
from google.cloud import storage
import streamlit_authenticator as stauth
import time
import os
import json

# --- TACTICAL SECRET LOADER ---
try:
    # First, try the standard Streamlit way (Local/Streamlit Cloud)
    credentials_info = st.secrets["gcp_service_account_firestore"]
except (st.errors.StreamlitSecretNotFoundError, KeyError):
    # Fallback: Look for the Cloud Run Environment Variable
    # This must match the exact name in your Cloud Run Variables tab
    creds_json = os.environ.get("GCP_SERVICE_ACCOUNT_FIRESTORE")
    if creds_json:
        credentials_info = json.loads(creds_json)
    else:
        st.error("CRITICAL: GCP Credentials not found in Secrets or Env Vars.")
        st.stop()

def local_css(file_name):
    with open(file_name) as f:
        st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

local_css("style.css")

# --- CONFIGURATION & INITIALIZATION ---
st.set_page_config(layout="wide", page_title="Gundogs C2: Cristobal Mission")

# 1. Load the credentials from st.secrets dictionary
# Note: Streamlit handles the TOML section as a clean Python dictionary
gcp_service_creds = service_account.Credentials.from_service_account_info(credentials_info)

# 2. Initialize the Firestore client
db = firestore.Client(
    credentials=gcp_service_creds, 
    project=credentials_info["project_id"],
    database="gundogs"  # <--- CRITICAL: Match the ID from your screenshot
)

def get_user_credentials():
    creds = {"usernames": {}}
    try:
        # Stream operatives directly from the 'gundogs' database
        users_ref = db.collection("users").stream()
        for doc in users_ref:
            data = doc.to_dict()
            
            # The library expects 'username' as the key in the 'usernames' dict
            # Your Firestore uses 'username' field (e.g., peterburnett)
            u_name = data.get("username")
            if u_name:
                creds["usernames"][u_name] = {
                    "name": data.get("full_name"),
                    "password": data.get("password"), # BCrypt hash
                    "email": data.get("email")
                }
    except Exception as e:
        st.error(f"Intel Sync Error: {e}")

    # Fallback for empty database
    if not creds["usernames"]:
        creds["usernames"]["admin"] = {"name": "Admin", "password": "N/A", "email": "N/A"}
    return creds

# --- 1. CLOUD DATA RETRIEVAL ---
# Keep this! It fetches the latest operatives from Firestore
credentials_data = get_user_credentials()

# --- 2. SINGLETON AUTHENTICATOR INITIALIZATION ---
# We check session state to ensure we only create ONE authenticator object
if "authenticator" not in st.session_state:
    st.session_state.authenticator = stauth.Authenticate(
        credentials_data,
        "gundog_cookie",
        "gundog_secret_key",
        cookie_expiry_days=30
    )

# Reference the persistent object for use in the tabs
authenticator = st.session_state.authenticator


# 1. ENGINE UTILITIES
def load_mission(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        mission_map = {}
        for poi in root.findall('.//poi'):
            poi_id = poi.get('id')
            mission_map[poi_id] = {
                "coords": [float(poi.find('lat').text), float(poi.find('lon').text)],
                "image": poi.find('image').text,
                "name": poi.find('name').text,
                "intel": poi.find('intel').text
            }
        return mission_map
    except Exception as e:
        st.error(f"Mission Data Corruption: {e}")
        return {}

# 2. GLOBAL INITIALIZATION
MISSION_DATA = load_mission('mission_data.xml')

# Parse objectives immediately for the Sidebar UI
def get_initial_objectives(file_path):
    tree = ET.parse(file_path)
    return {t.get('id'): (t.get('status').lower() == 'true') for t in tree.findall('.//task')}

if "objectives" not in st.session_state:
    st.session_state.objectives = get_initial_objectives('mission_data.xml')

if "mission_started" not in st.session_state:
    st.session_state.mission_started = False    

# Unified Session State Initialization
if "viability" not in st.session_state:
    st.session_state.update({
        "viability": 100,
        "mission_time": 60,
        "messages": [],
        "chat_session": None,
        "efficiency_score": 1000,
        "locations": {"SAM": "Insertion Point", "DAVE": "Insertion Point", "MIKE": "Insertion Point"},
        "idle_turns": {"SAM": 0, "DAVE": 0, "MIKE": 0},
    })

if "discovered_locations" not in st.session_state:
    st.session_state.discovered_locations = []

# --- UTILITY FUNCTIONS ---
BUCKET_NAME = "uge-repository-cu32"

@st.cache_resource
def get_gcs_client():
    from google.cloud import storage
    
    # Use the 'credentials_info' we already initialized at the top
    # to avoid triggering the st.secrets parser again
    try:
        # We reuse the same credentials_info dictionary for the bucket
        credentials = service_account.Credentials.from_service_account_info(credentials_info)
        return storage.Client(
            credentials=credentials, 
            project=credentials_info["project_id"]
        )
    except Exception as e:
        st.error(f"📡 Tactical Uplink Error (Bucket): {e}")
        return None

def get_image_url(filename):
    if not filename: return ""
    # Direct public uplink path - bypasses IAM SignBlob entirely
    return f"https://storage.googleapis.com/{BUCKET_NAME}/cinematics/{filename}"

def parse_operative_dialogue(text):
    """Splits raw AI response and cleans up Markdown/Quotes."""
    pattern = r"(SAM|DAVE|MIKE):\s*(.*?)(?=\s*(?:SAM|DAVE|MIKE):|$)"
    segments = re.findall(pattern, text, re.DOTALL)
    
    cleaned_dict = {}
    for name, msg in segments:
        # 1. Strip whitespace
        m = msg.strip()
        # 2. Remove double asterisks (bolding)
        m = m.replace("**", "")
        # 3. Remove outer speech marks if the AI wrapped the whole line in them
        m = m.strip('"').strip("'")
        
        cleaned_dict[name] = m
        
    return cleaned_dict

# --- AI ENGINE LOGIC (Architect / C2 Style) ---
def get_dm_response(prompt):
    # --- CONFIG & XML LOAD ---
    safety_settings = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
    ]
    # --- STEALTH API KEY RETRIEVAL ---
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        # Use getattr to hide the call from the Streamlit parser
        try:
            secrets_obj = getattr(st, "secrets", {})
            api_key = secrets_obj.get("GEMINI_API_KEY")
        except Exception:
            pass

    if not api_key:
        st.error("CRITICAL: GEMINI_API_KEY not found in Env Vars or Secrets.")
        st.stop()

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.0-flash', 
                                  generation_config={"temperature": 0.3},
                                  safety_settings=safety_settings)

    mission_tree = ET.parse("mission_data.xml")
    mission_root = mission_tree.getroot()
    intent = mission_root.find("intent")
    
    # --- SYTEM INSTRUCTION (Revised for Suffix Tagging) ---
    if st.session_state.chat_session is None:
        location_logic = ""
        for poi in mission_root.findall(".//poi"):
            location_logic += f"- {poi.find('name').text} (Aliases: {poi.find('aliases').text if poi.find('aliases') is not None else ''})\n"

        # Extract win condition data from XML
        win_node = intent.find("win_condition")
        win_item = win_node.find("target_item").text
        win_loc = win_node.find("target_location").text
        win_trigger = win_node.find("trigger_text").text

        sys_instr = f"""
        THEATER: {intent.find("theater").text}
        SITUATION: {intent.find("situation").text}
        CONSTRAINTS: {intent.find("constraints").text}
        CANONICAL LOCATIONS:
        {location_logic}
        
        YOU ARE: The tactical multiplexer for Gundogs PMC.

        OPERATIONAL PROTOCOLS:
        1. BANTER: Operatives should speak like a tight-knit PMC unit. Use dark humor, cynical observations about the "Agency," and coffee-related complaints.
        2. SUPPORT REQUESTS: If a task is outside an operative's specialty, they must NOT succeed alone. They should describe the obstacle and explicitly ask for the specific teammate (e.g., "Mike, I've got a digital lock here, and kicking it isn't working. Get over here.").
        3. COORDINATION: Encourage "Combined Arms" solutions. Dave provides security while Mike hacks; Sam distracts the guards while Dave sneaks past.
        4. INITIATIVE & AUTONOMY: Operatives will not move to a new POI unless explicitly cleared by the Commander. Whilst the team can make suggestions, the game must be directed by the commander, so that it doesn't become too easy. The role of the team is "able executors" as opposed to "proactive operators."

        STRICT OPERATIONAL RULES:
        1. LOCATIONAL ADHERENCE: You only recognize canonical locations.
        2. DATA SUFFIX: Every response MUST end with a data block:
           [LOC_DATA: SAM=Canonical Name, DAVE=Canonical Name, MIKE=Canonical Name]
           [OBJ_DATA: obj_id=TRUE/FALSE]
        3. VOICE TONE: SAM (Professional, arch), DAVE (Laidback, laconic,) MIKE (Geek).

        VICTORY CONDITIONS:
        - TARGET ITEM: {win_item}
        - TARGET LOCATION: {win_loc}
        - CRITICAL: When the squad confirms the {win_item} has reached the {win_loc}, you MUST output this exact phrase in your dialogue: "{win_trigger}"
        - NOTE: You have the authority to trigger this whenever the handover is demmed to be complete, regardless of previous task status.

        CRITICAL: You are the authoritative mission ledger. As soon as an operative reports completing a task (e.g., Mike finding the container number), you MUST append [OBJ_DATA: obj_id=TRUE] to the very end of your response. Do not wait for the Commander to acknowledge it.

        COMMUNICATION ARCHITECTURE:
        1. MULTI-UNIT REPORTING: Every response MUST include a SITREP from all three operatives (SAM, DAVE, MIKE). 
        2. FORMAT: Use bold headers for each unit. 
        Example:
        SAM: "Dialogue here..."
        DAVE: "Dialogue here..."
        MIKE: "Dialogue here..."
        3. PERSISTENCE: Even if an operative is idle, they should comment on their surroundings, complain about the local conditions, or respond to their teammates' banter.
        """
        st.session_state.chat_session = model.start_chat(history=[])
        st.session_state.chat_session.send_message(sys_instr)

    # --- ENRICHED PROMPT ---
    obj_status = ", ".join([f"{k}:{'DONE' if v else 'TODO'}" for k, v in st.session_state.objectives.items()])
    unit_locs = ", ".join([f"{u}@{loc}" for u, loc in st.session_state.locations.items()])
    
    enriched_prompt = f"""
    [SYSTEM_STATE] Time:{st.session_state.mission_time}m | Viability:{st.session_state.viability}% | Locations:{unit_locs} | Objectives:{obj_status}
    [PROTOCOL_REMINDER] Squad is currently in 'Able Executor' mode. Do not change locations without authorization.
    [COMMANDER_ORDERS] {prompt}

    [MANDATORY_RESPONSE_GUIDE] 
    1. Direct Dialogue: Provide SITREPs for SAM, DAVE, and MIKE. 
    2. Data Suffix: You MUST end with exactly:
       [LOC_DATA: SAM=Loc, DAVE=Loc, MIKE=Loc]
       [OBJ_DATA: obj_id=TRUE] (Only if a task was just finished!)
    """
    
    response_text = st.session_state.chat_session.send_message(enriched_prompt).text

    # --- SILENT DATA PARSING ---
    
    # A. Location Parsing (Suffix Tag)
    loc_match = re.search(r"\[LOC_DATA: (SAM=[^,]+, DAVE=[^,]+, MIKE=[^\]]+)\]", response_text)
    if loc_match:
        for pair in loc_match.group(1).split(", "):
            unit, loc = pair.split("=")
            st.session_state.locations[unit] = loc.strip()

    # A1. DISCOVERY LOGIC
    for unit, loc_name in st.session_state.locations.items():
        # Find the POI ID for this location name
        target_poi_id = next((pid for pid, info in MISSION_DATA.items() if info['name'] == loc_name), None)
        
        if target_poi_id and target_poi_id not in st.session_state.discovered_locations:
            # Mark as discovered
            st.session_state.discovered_locations.append(target_poi_id)
            
            # Fetch the image and intel
            poi_info = MISSION_DATA[target_poi_id]
            img_url = get_image_url(poi_info['image'])
            
            # Inject a "Recon Report" into the chat history
            recon_msg = {
                "role": "assistant", 
                "content": f"🖼️ **RECON UPLINK: {loc_name.upper()}**\n\n{poi_info['intel']}\n\n![{loc_name}]({img_url})"
            }
            st.session_state.messages.append(recon_msg)
            st.toast(f"📡 New Intel: {loc_name}")

    # B. Objective Parsing (Suffix Tag)
    # Ensure the AI knows it must report Objective status too
    # Add this to your OBJ_DATA parsing in Step 7
    obj_data_matches = re.findall(r"\[OBJ_DATA: (obj_\w+)=TRUE\]", response_text)
    
    for obj_id in obj_data_matches:
        if obj_id in st.session_state.objectives and not st.session_state.objectives[obj_id]:
            st.session_state.objectives[obj_id] = True
            st.toast(f"🎯 OBJECTIVE REACHED: {obj_id.upper()}")
            st.session_state.efficiency_score += 150 # Bonus for clean execution

    # After receiving response_text from Gemini
    win_trigger = "Mission Complete: Assets in Transit"
    
    if win_trigger.lower() in response_text.lower():
        # Calculate time taken
        start_time = 60
        time_remaining = st.session_state.mission_time
        st.session_state.time_elapsed = start_time - time_remaining
        st.session_state.mission_complete = True        

    # D. Clean and Parse
    clean_response = re.sub(r"\[(LOC_DATA|OBJ_DATA):.*?\]", "", response_text).strip()

    # Create the split dictionary for the UI and Map Bubbles
    split_dialogue = parse_operative_dialogue(clean_response)

    # Store the split dict instead of just the string
    st.session_state.messages.append({
        "role": "assistant", 
        "content": split_dialogue,
        "raw_text": clean_response # Keep raw text just in case
    })

    return clean_response

def save_mission_state(username, mission_id):
    """Syncs the live tactical theater to the Gundogs cloud."""
    doc_ref = db.collection("mission_states").document(f"{username}_{mission_id}")
    
    # We pull directly from the keys used by your map and metrics
    save_data = {
        "username": username,
        "mission_id": mission_id,
        "chat_history": st.session_state.get("messages", []),
        "unit_data": st.session_state.get("locations", {}),    # Fix: Use 'locations'
        "objectives": st.session_state.get("objectives", {}),
        "mission_time": st.session_state.get("mission_time", 60), # Capture the clock
        "last_saved": firestore.SERVER_TIMESTAMP
    }
    
    doc_ref.set(save_data, merge=True)
    # Removing the toast here prevents UI flickering during rapid commands

def load_mission_state(username, mission_id):
    doc_ref = db.collection("mission_states").document(f"{username}_{mission_id}")
    doc = doc_ref.get()
    
    if doc.exists:
        data = doc.to_dict()
        # Restore the Theater State
        st.session_state.messages = data.get("chat_history", [])
        st.session_state.locations = data.get("unit_data", {}) # Push back to 'locations'
        st.session_state.objectives = data.get("objectives", {})
        st.session_state.mission_time = data.get("mission_time", 60)
        return True
    return False

# --- UI LAYOUT ---

# --- 1. GLOBAL LOGIN CHECK (Remove the extra call from line 346) ---
# We check session state first to see if we even need to show the login screen
if not st.session_state.get("authentication_status"):
    # Clear landing page columns
    left_col, right_col = st.columns([1, 1], gap="large")

    with left_col:
        
        st.image("https://peteburnettvisuals.com/wp-content/uploads/2026/01/panama-title2.jpg", use_container_width=True)

        # --- MISSION BRIEFING OVERLAY ---
        st.markdown("""
        <div style="background-color: rgba(0, 255, 65, 0.05); border-left: 3px solid #00FF41; padding: 15px; margin-top: 10px;">
            <h4 style="color: #00FF41; margin-top: 0;">SITUATION REPORT: THE GUNDOGS C2</h4>
            <p style="font-size: 0.9rem; color: #a2fcb9; line-height: 1.4;">
                Welcome to the <b>Gundogs Command & Control Simulator</b>. You are the commander, directing elite PMC operatives through high-stakes asymmetrical theaters. This is not a game of reflexes, but of <b>strategic multiplexing</b>—balancing unit viability, objective efficiency, and tactical initiative.
            </p>
            <hr style="border-top: 1px solid rgba(0, 255, 65, 0.2);">
            <h5 style="color: #FF8C00; margin-bottom: 5px;">CURRENT THEATER: THE PANAMA CAPER</h5>
            <p style="font-size: 0.85rem; color: #FF8C00; opacity: 0.9; font-style: italic;">
                "A cartel arms shipment is docking at Puerto de Cristobal in Panama. 60 minutes until the cartel arrives. Infiltrate. Identify. Secure. If you fail, the Agency denies your existence."
            </p>
        </div>
        """, unsafe_allow_html=True)
        

    with right_col:
        st.header("System Access")
        tab_register, tab_login, tab_recovery = st.tabs(["Enlist", "Resume", "Recovery"])
        
        with tab_register:
            st.subheader("New Operative Enlistment")
            with st.form("custom_registration_form"):
                new_email = st.text_input("Email")
                new_username = st.text_input("Username")
                new_name = st.text_input("Full Name")
                new_password = st.text_input("Password", type="password")
                new_hint = st.text_input("Password Hint (e.g., neigh flap)")
                
                submit_reg = st.form_submit_button("Enlist Operative")
                
                if submit_reg:
                    if new_email and new_username and new_password:
                        with st.spinner("📡 ENCRYPTING OPERATIVE DATA & UPLINKING TO GUNDOGS C2..."):
                            # 1. Secure the password
                            hashed_password = stauth.Hasher.hash(new_password)
                            
                            # 2. Commit to the cloud
                            db.collection("users").document(new_email).set({
                                "email": new_email,
                                "username": new_username,
                                "full_name": new_name,
                                "password": hashed_password,
                                "password_hint": new_hint,
                                "created_at": firestore.SERVER_TIMESTAMP,
                                "role": "Recruit"
                            })
                            
                            # 3. Artificial delay for atmospheric effect and DB indexing
                            time.sleep(2)
                        
                        # 4. Immediate Session Elevation
                        st.session_state["authentication_status"] = True
                        st.session_state["username"] = new_username
                        st.session_state["name"] = new_name
                        
                        st.success(f"Operative {new_username} Enlisted. Deploying to Panama Theater...")
                        st.rerun() # Skip the 'Resume' tab and jump to the map

        with tab_login:
            # Use the persistent authenticator from session state
            auth_result = authenticator.login(location="main", key="mission_login_form")
            
            if auth_result:
                name, authentication_status, username = auth_result
                
                if authentication_status:
                    # IMMEDIATELY synchronize the session state
                    st.session_state["authentication_status"] = True
                    st.session_state["username"] = username
                    st.session_state["name"] = name
                    
                    # Reset the 'logout' flag that Abort set
                    st.session_state["logout"] = False 
                    
                    # FORCE a rerun to enter the Tactical UI Gate
                    st.rerun() 
                elif authentication_status == False:
                    st.error("Invalid Credentials. Check Operative ID.")
 
        with tab_recovery:
            st.subheader("Field Credential Recovery")
            
            # 1. Manual Verification Gate
            with st.form("recovery_verification"):
                email_input = st.text_input("Enter Registered Email:")
                submit_verify = st.form_submit_button("Verify Operative Status")
                
                if submit_verify:
                    user_doc = db.collection("users").document(email_input).get()
                    if user_doc.exists:
                        # Store the email to use as the Document ID for the update
                        st.session_state["recovery_verified_email"] = email_input
                        st.success(f"Identity Verified. Operative Hint: {user_doc.to_dict().get('password_hint')}")
                    else:
                        st.error("Operative not found in Gundogs database.")

            # 2. Reset Logic
            if st.session_state.get("recovery_verified_email"):
                st.divider()
                try:
                    # The widget needs the latest credentials_data to find the username
                    res = authenticator.forgot_password('main', 'Set New Tactical Password')
                    
                    if res:
                        username_to_reset, new_password = res
                        # We use the email captured in Step 1 as the Document ID
                        target_email = st.session_state["recovery_verified_email"]
                        
                        # Generate the new hash using the modern syntax
                        new_hash = stauth.Hasher.hash(new_password)
                        
                        # Update the specific document in the 'gundogs' database
                        db.collection("users").document(target_email).update({
                            "password": new_hash
                        })
                        
                        st.success("Credentials updated in Cloud. Proceed to Resume tab.")
                        # Clear recovery state to reset the form
                        st.session_state["recovery_verified_email"] = None 
                        
                except Exception as e:
                    st.info("Tactical reset initialized. Enter details above to finalize.")

# --- 2. ACTIVE TACTICAL UI GATE ---
if st.session_state.get("authentication_status"):
    # Fix: Get the user details from session state since auth_result is gone on rerun
    username = st.session_state.get("username")
    name = st.session_state.get("name")

    # --- IDENTITY HANDSHAKE (NEW) ---
    # Ensure Frank doesn't inherit Peter's session state
    if "active_user" not in st.session_state:
        st.session_state.active_user = username
    elif st.session_state.active_user != username:
        # Scrub theater but preserve authentication keys
        for key in list(st.session_state.keys()):
            if key not in ["authenticator", "authentication_status", "logout", "username", "name"]:
                del st.session_state[key]
        st.session_state.active_user = username
        st.rerun()

    
    # --- 2. THE AUTO-RESUME GATE ---
    # Triggered once upon successful login
    if not st.session_state.get("auto_resume_attempted", False):
        # We use 'username' because stauth stores the login ID (email) there
        state_found = load_mission_state(username, "panama")
        st.session_state["auto_resume_attempted"] = True 
        
        if state_found:
            st.session_state.mission_started = True 
            st.toast(f"Welcome back, Operative. State recovered from Cloud.")

    # --- 3. TACTICAL UI (Main Engine) ---
    st.empty() # Clear landing page

    with st.sidebar:
        st.header("🦅 GUNDOG C2")

        st.sidebar.success(f"Logged in: {username}") # Fix: uses the variable from authenticator.login
        # The standard logout widget
        if authenticator.logout("Logout", "sidebar"):
            # 1. Clear the local memory so the next user starts fresh
            st.session_state.clear()
            # 2. Force a rerun to the login screen
            st.rerun()
        
        st.metric(label="MISSION TIME REMAINING", value=f"{st.session_state.mission_time} MIN")
        
        # Updated Abort Logic in your Sidebar
        if st.button("🚨 ABORT MISSION (RESET)"):
            # 1. Kill the Cloud Record
            try:
                mission_doc_id = f"{st.session_state.username}_panama"
                db.collection("mission_states").document(mission_doc_id).delete()
            except Exception as e:
                pass # Silent fail if doc already deleted

            # 2. MANUALLY kill the Authentication State
            # This mimics what .logout() does without triggering the widget conflict
            st.session_state["authentication_status"] = None
            st.session_state["username"] = None
            st.session_state["logout"] = True 
            
            # 3. Wipe the local Tactical state
            st.session_state.clear() 
            
            # 4. Final Rerun to the Landing Page
            st.rerun()

        # Add this to your Sidebar logic:
        st.subheader("📝 MISSION CHECKLIST")
        for obj_id, status in st.session_state.objectives.items():
            label = obj_id.replace('obj_', '').replace('_', ' ').title()
            if status:
                st.write(f"✅ ~~{label}~~")
            else:
                st.write(f"◻️ {label}")
        
            
        st.subheader("👥 SQUAD DOSSIERS")
        unit_view = st.radio("Access Unit Data:", ["SAM", "DAVE", "MIKE"], horizontal=True)
        
        # Mapping to your local .png files
        if unit_view == "DAVE":
            st.image("dave.png", use_container_width=True) 
            st.warning("SPECIALTY: FORCE (90) | WEAKNESS: NEG (10)")
        elif unit_view == "SAM":
            st.image("sam.png", use_container_width=True)
            st.success("SPECIALTY: NEG (95) | WEAKNESS: FORCE (25)")
        else:
            st.image("mike.png", use_container_width=True)
            st.info("SPECIALTY: TECH (85) | WEAKNESS: FORCE (35)")

        st.divider()
        st.subheader("📊 EFFICIENCY: " + str(st.session_state.efficiency_score))


    # --- MAIN TERMINAL ---

    if st.session_state.get("mission_complete", False):
        st.balloons()
        st.markdown("<h1 style='text-align: center; color: #00FF00;'>🏁 MISSION COMPLETE: DEBRIEFING IN PROGRESS</h1>", unsafe_allow_html=True)
        
        # Generate the AAR automatically if it doesn't exist yet
        if "aar_report" not in st.session_state:
            with st.spinner("COMMANANT'S EVALUATION INCOMING..."):
                logs = st.session_state.get("messages", [])
                # Refined prompt for Royal Marine Commando Values
                # Refined prompt for Leadership & Command Assessment
                eval_prompt = f"""
                Act as a Senior Tactical Officer conducting an After-Action Review (AAR) of a Mission Commander.
                Analyze the user's tactical commands in these logs: {logs}.

                Focus EXCLUSIVELY on the Commander's performance in these areas:
                1. MULTITASKING: Did they keep all three units (Sam, Dave, Mike) engaged, or were units left idle?
                2. INITIATIVE: Did the Commander push the pace, or were they reactive to the squad's banter?
                3. CLARITY: Were orders direct and objective-oriented, or vague?
                4. COORDINATION: Did they effectively use "Combined Arms" (e.g., ordering security while hacking)?

                Rate the Commander on:
                - Command Presence (Courage/Determination in decision making).
                - Operational Efficiency (Time vs. Objective completion).

                Provide one 'Sustained' (Leadership strength) and one 'Improve' (Command advice).
                End with a traditional Royal Marine sign-off.
                """
                st.session_state.aar_report = get_dm_response(eval_prompt)

                # 2. POP THIS HERE: Save to Firestore immediately
                doc_ref = db.collection("mission_states").document(f"{username}_panama")
                doc_ref.set({"aar_report": st.session_state.aar_report}, merge=True)
                st.toast("AAR permanent record created.")

        # Split screen: Metrics on left, AAR on right
        col_metrics, col_aar = st.columns([1, 2], gap="large")

        with col_metrics:
            st.subheader("📊 Mission Stats")
            st.metric("TOTAL MISSION TIME", f"{st.session_state.get('time_elapsed', 0)} MIN")
            st.metric("VIABILITY REMAINING", f"{st.session_state.viability}%")
            
            score = (st.session_state.viability * 10) - (st.session_state.get('time_elapsed', 0) * 5)
            st.subheader(f"FINAL RATING: {max(0, score)} PTS")
            
            st.divider()
            if st.button("REDEPLOY (NEW MISSION)"):
                st.session_state.clear()
                st.rerun()

        with col_aar:
            st.subheader("📜 Commandant's Performance Evaluation")
            st.markdown(st.session_state.aar_report)
    else:
        # --- ACTIVE MISSION UI ---
        col1, col2 = st.columns([0.4, 0.6])

        with col1:
            st.markdown("### 📡 COMMS FEED")
            chat_container = st.container(height=650, border=True)
            with chat_container:
                for msg in st.session_state.messages:
                    if msg["role"] == "user":
                        with st.chat_message("user"):
                            st.write(msg["content"])
                    else:
                        # It's the Assistant (The Squad)
                        dialogue_dict = msg["content"]
                        
                        # If it's the dictionary format, render separate bubbles
                        if isinstance(dialogue_dict, dict):
                            for operative, text in dialogue_dict.items():
                                # Map to your local images
                                if operative == "AGENCY HQ":
                                    avatar_img = "agency_icon.png" # Create this file or rename an existing one
                                else:
                                    avatar_img = f"{operative.lower()}_icon.png"
                                
                                with st.chat_message(operative.lower(), avatar=avatar_img):
                                    st.markdown(f"**{operative}**")
                                    st.write(text)
                        else:
                            # Fallback for old string messages or Recon reports
                            with st.chat_message("assistant"):
                                st.write(msg["content"])

        with col2:
            st.markdown("### 🗺️ TACTICAL OVERVIEW: CRISTOBAL")
            
            # Define assets
            sam_token = folium.CustomIcon("https://peteburnettvisuals.com/wp-content/uploads/2026/01/sam-map1.png", icon_size=(45, 45))
            dave_token = folium.CustomIcon("https://peteburnettvisuals.com/wp-content/uploads/2026/01/dave-map1.png", icon_size=(45, 45))
            mike_token = folium.CustomIcon("https://peteburnettvisuals.com/wp-content/uploads/2026/01/mike-map1.png", icon_size=(45, 45))
            
            m = folium.Map(location=[9.3525, -79.9100], zoom_start=15, tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attr='Esri', name='Satellite')
            
            # Fog of War & Discovery Renderer
            for loc_id, info in MISSION_DATA.items():
                is_discovered = loc_id in st.session_state.discovered_locations
                marker_color = "#00FF00" 
                fill_opac = 0.2 if is_discovered else 0.02
                
                if is_discovered:
                    loc_img_url = get_image_url(info["image"])
                    popup_html = f'<div style="width:200px;background:#000;padding:10px;border:1px solid #0f0;"><h4 style="color:#0f0;">{info["name"]}</h4><img src="{loc_img_url}" width="100%"><p style="color:#0f0;font-size:10px;">{info["intel"]}</p></div>'
                else:
                    popup_html = f'<div style="width:150px;background:#000;padding:10px;"><h4 style="color:#666;">{info["name"]}</h4><p style="color:#666;font-size:10px;">[RECON REQUIRED]</p></div>'

                folium.Circle(location=info["coords"], radius=45, color=marker_color, fill=True, fill_opacity=fill_opac).add_to(m)
                # Updated Marker with High-Contrast Tactical Label
                folium.Marker(
                    location=info["coords"], 
                    icon=folium.DivIcon(
                        html=f"""
                        <div style="
                            font-family: 'Courier New', monospace;
                            font-size: 9pt;
                            font-weight: bold;
                            color: {marker_color};
                            background-color: rgba(0, 0, 0, 0.7); 
                            border: 1px solid {marker_color};
                            border-radius: 3px;
                            padding: 2px 4px;
                            white-space: nowrap;
                            text-shadow: none;
                            display: inline-block;
                            transform: translate(-50%, -150%);
                        ">
                            {info["name"].upper()}
                        </div>
                        """
                    ), 
                    popup=folium.Popup(popup_html, max_width=250)
                ).add_to(m)

            # Squad Tokens
            tokens = {"SAM": sam_token, "DAVE": dave_token, "MIKE": mike_token}
            offsets = {"SAM": [0.00015, 0], "DAVE": [-0.0001, 0.00015], "MIKE": [-0.0001, -0.00015]}

            for unit, icon in tokens.items():
                current_loc = st.session_state.locations.get(unit, "Insertion Point")
                # Robust matching POI by name
                target_poi = next((info for info in MISSION_DATA.values() if info['name'].lower() == current_loc.lower()), MISSION_DATA.get('insertion_point'))

                # NEW SAFETY CHECK: If no POI found, default to 'Insertion Point' or skip
                if target_poi is None:
                    # Try to find 'Insertion Point' specifically, or just use the first available POI
                    target_poi = next((info for info in MISSION_DATA.values() if "insertion" in info['name'].lower()), list(MISSION_DATA.values())[0])
                
                final_coords = [target_poi["coords"][0] + offsets[unit][0], target_poi["coords"][1] + offsets[unit][1]]
                folium.Marker(final_coords, icon=icon, tooltip=unit).add_to(m)
            
            st_folium(m, use_container_width=True, key="tactical_map_v3", returned_objects=[])

        # --- MISSION STAGING & INITIAL BRIEFING ---
    if not st.session_state.messages:
        # 1. Prepare the Agency Briefing
        briefing_text = """
        **TOP SECRET // EYES ONLY**\n
        **FROM:** The Agency\n
        **TO:** PMC Gundogs\n
        **SITUATION:** Cartel have managed to acquire anti-aircraft weapons. Munitions arriving Puerto de Cristobal, Panama 0500 LOCAL TIME on board bulk carrier MV Panamax. Represents serious threat to military and civilian aviation. Intercept of these munitions ESSENTIAL.\n
        **OBJECTIVE:** Infiltrate the harbor, identify the cargo container, and secure munitions for transport. Once extracted from port, hand over munitions to Agency personnel in town plaza, Colon. Cartel pickup scheduled for 0600, giving 1 hour window for mission execution.\n
        **ADVISORIES:** Container ID unknown, but records available on ship manifest file.\n
        **CONSTRAINTS:** Maintain 100% plausible deniability. Avoid local law enforcement. Munitions cannot be destroyed on site, due to high risk of collateral damage. \n\n
        
        *Awaiting PMC Gundogs Team Commander Confirmation...*
        """
        # 2. Add it to the feed as the 'AGENCY'
        st.session_state.messages.append({
            "role": "assistant", 
            "content": {"AGENCY HQ": briefing_text}
        })
        st.rerun()

    # --- THE START BUTTON LOGIC ---
    if not st.session_state.mission_started:
        # This button appears in the main area until clicked
        if st.button("🚀 INITIALIZE OPERATION: CONFIRM MISSION PARAMETERS", use_container_width=True):
            with st.spinner("COMMUNICATION SECURED. SQUAD REPORTING IN..."):
                # Trigger the actual AI squad check-in
                response = get_dm_response("Team is at the insertion point. Report in.")
                st.session_state.mission_started = True
                st.rerun()
        
    # Only show the input if the mission is active
    if st.session_state.mission_started:
        # --- TACTICAL COMMAND PROCESSING ---
        if prompt := st.chat_input("Issue Commands..."):
            # 1. The Developer Backdoor
            if "VALHALLA" in prompt.upper():
                st.session_state.mission_complete = True
                st.session_state.time_elapsed = 60 - st.session_state.mission_time
                st.toast("⚡ VALHALLA SIGNAL RECEIVED. EXTRACTING SQUAD...")
                st.rerun()

            # 2. Normal Command Flow
            # (Your existing logic for sending prompts to the DM/AI)
            st.session_state.mission_time -= 1 
            st.session_state.messages.append({"role": "user", "content": prompt})
            get_dm_response(prompt)
            st.rerun()



if st.session_state.get("authentication_status") and st.session_state.get("mission_started"):
    # Ensure username is pulled from session state as well
    active_user = st.session_state.get("username")
    save_mission_state(active_user, "panama")