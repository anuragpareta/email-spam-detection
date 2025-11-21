# app/main.py

import os
import io
import json
import datetime
import uuid
from pathlib import Path
from datetime import datetime as dt, timedelta
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from app.services.gmail_service import GmailService
from app.services.spam_classifier import SpamClassifier
import pandas as pd
from dotenv import load_dotenv

# ============================================
# CONFIGURATION
# ============================================

load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # Remove in production with HTTPS

try:
    BASE_DIR = Path(__file__).resolve().parent
except NameError:
    BASE_DIR = Path(os.getcwd()).resolve()

STATIC_DIR = BASE_DIR / "static"

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/callback")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    raise ValueError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in environment variables")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

client_config = {
    "web": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uris": [GOOGLE_REDIRECT_URI],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token"
    }
}

# ============================================
# IN-MEMORY CACHE FOR EMAIL DATA
# ============================================

email_cache = {}

def store_user_emails(user_id: str, emails: list, source: str = "model prediction"):
    """Store emails in memory cache with expiration."""
    email_cache[user_id] = {
        "emails": emails,
        "source": source,
        "created_at": dt.now(),
        "expires_at": dt.now() + timedelta(hours=2)
    }
    print(f"📦 Cached {len(emails)} emails for user {user_id[:8]}... (expires in 2 hours)")

def get_user_emails(user_id: str):
    """Get emails from cache if not expired."""
    if user_id not in email_cache:
        return None, None
    
    cached = email_cache[user_id]
    if dt.now() > cached["expires_at"]:
        del email_cache[user_id]
        print(f"⏰ Cache expired for user {user_id[:8]}...")
        return None, None
    
    return cached["emails"], cached["source"]

def cleanup_expired_cache():
    """Remove expired cache entries."""
    expired = [uid for uid, data in email_cache.items() if dt.now() > data["expires_at"]]
    for uid in expired:
        del email_cache[uid]
    if expired:
        print(f"🧹 Cleaned up {len(expired)} expired cache entries")

# ============================================
# FASTAPI APP
# ============================================

app = FastAPI(title="Email Spam Detection")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "change-this-in-production-to-random-string"),
    max_age=900,  # 15 minutes (backend backup)
    session_cookie="spam_detector_session"
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

spam_classifier = SpamClassifier()

# ============================================
# HELPER FUNCTIONS
# ============================================

def get_gmail_service_from_session(request: Request) -> GmailService:
    """Get authenticated GmailService for current user from session."""
    creds_json = request.session.get("credentials")
    if not creds_json:
        raise HTTPException(status_code=401, detail="Not authenticated. Please authorize with Gmail first.")
    
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)
    
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            request.session["credentials"] = creds.to_json()
        else:
            raise HTTPException(status_code=401, detail="Token expired. Please re-authorize.")
    
    return GmailService(credentials=creds)

def get_or_create_user_id(request: Request) -> str:
    """Get or create unique user ID for cache."""
    if "user_id" not in request.session:
        request.session["user_id"] = str(uuid.uuid4())
    return request.session["user_id"]

def _get_active_results(request: Request):
    """Get active results from cache."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None, None
    
    cleanup_expired_cache()
    return get_user_emails(user_id)

# ============================================
# ROUTES
# ============================================

@app.get("/", response_class=HTMLResponse)
async def home():
    """Home page."""
    try:
        with open(STATIC_DIR / "home.html") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return JSONResponse({
            "message": "Email Spam Detection - Visit /authorize to begin",
            "next_step": "POST /authorize"
        })


@app.post("/authorize")
async def authorize():
    """Initiate OAuth flow."""
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    auth_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    return RedirectResponse(auth_url)


@app.get("/callback", response_class=HTMLResponse)
async def callback(request: Request):
    """OAuth callback."""
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    flow.fetch_token(authorization_response=str(request.url))
    credentials = flow.credentials
    request.session["credentials"] = credentials.to_json()
    
    path = STATIC_DIR / "date_range.html"
    try:
        with open(path) as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return JSONResponse({
            "status": "success",
            "message": "Authorization successful! Go to /fetch-emails to continue",
            "next_step": "POST /fetch-emails with start_date and end_date"
        })


@app.post("/fetch-emails", response_class=HTMLResponse)
async def fetch_emails(request: Request, start_date: str = Form(...), end_date: str = Form(...)):
    """Fetch and classify emails."""
    gmail_service = get_gmail_service_from_session(request)
    user_id = get_or_create_user_id(request)
    
    def normalize(s: str) -> str:
        for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(s, fmt).strftime("%d-%m-%Y")
            except ValueError:
                pass
        raise HTTPException(status_code=400, detail="Dates must be DD-MM-YYYY or YYYY-MM-DD")
    
    sd = normalize(start_date)
    ed = normalize(end_date)
    
    try:
        emails = gmail_service.fetch_emails_by_date_range(sd, ed)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gmail fetch failed: {str(e)}")
    
    if not emails:
        return JSONResponse({
            "status": "success",
            "message": "No emails found for this date range",
            "total": 0,
            "spam": 0
        })
    
    spam_count = 0
    for email in emails:
        label = spam_classifier.classify_email(
            email.get("sender", ""),
            email.get("subject", ""),
            email.get("body", "")
        )
        email["prediction"] = label
        if label.lower() == "spam":
            spam_count += 1
    
    total_count = len(emails)
    
    # Store in memory cache
    store_user_emails(user_id, emails, source="model prediction")
    print(f"✅ Stored {total_count} emails in cache for user {user_id[:8]}...")
    
    try:
        page = (STATIC_DIR / "spam_result.html").read_text(encoding="utf-8")
        page = page.replace("{{ total_count }}", str(total_count))
        page = page.replace("{{ spam_count }}", str(spam_count))
        return HTMLResponse(page)
    except FileNotFoundError:
        return JSONResponse({
            "status": "success",
            "message": f"Classified {total_count} emails",
            "total": total_count,
            "spam": spam_count,
            "not_spam": total_count - spam_count,
            "date_range": f"{sd} to {ed}"
        })


@app.get("/download-results")
async def download_results(request: Request):
    """Download Excel results."""
    emails, source = _get_active_results(request)
    
    if not emails:
        raise HTTPException(status_code=404, detail="No results found. Please run detection first.")
    
    df = pd.DataFrame(emails)
    column_order = ["id", "sender", "subject", "body", "prediction"]
    df = df[[col for col in column_order if col in df.columns]]
    
    output = io.BytesIO()
    df.to_excel(output, index=False, engine='openpyxl')
    output.seek(0)
    
    filename = "spam_results_corrected.xlsx" if source == "user corrected" else "spam_results.xlsx"
    
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/upload-corrections")
async def upload_corrections(request: Request, file: UploadFile = File(...)):
    """Upload corrected Excel file."""
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file.")
    
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="No active session. Please run detection first.")
    
    contents = await file.read()
    
    try:
        df = pd.read_excel(io.BytesIO(contents))
        df.columns = [c.strip().lower() for c in df.columns]
        
        if not all(col in df.columns for col in ["id", "prediction"]):
            raise HTTPException(status_code=400, detail="Excel must contain columns: id, prediction")
        
        corrected_emails = df.to_dict(orient='records')
        
        # Store corrected version in cache
        store_user_emails(user_id, corrected_emails, source="user corrected")
        
        spam_count = sum(1 for e in corrected_emails if str(e.get("prediction", "")).lower().strip() == "spam")
        total_count = len(corrected_emails)
        
        print(f"✅ User uploaded corrections: {spam_count} spam out of {total_count} emails")
        
        page = (STATIC_DIR / "spam_result.html").read_text(encoding="utf-8")
        page = page.replace("{{ total_count }}", str(total_count))
        page = page.replace("{{ spam_count }}", str(spam_count))
        return HTMLResponse(page)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to process Excel: {str(e)}")


@app.post("/move-to-trash")
async def move_to_trash(request: Request):
    """Move spam emails to trash."""
    gmail_service = get_gmail_service_from_session(request)
    emails, source = _get_active_results(request)
    
    if not emails:
        raise HTTPException(status_code=404, detail="No results found. Please run detection first.")
    
    spam_ids = [str(e["id"]) for e in emails if str(e.get("prediction", "")).lower().strip() == "spam"]
    
    if not spam_ids:
        return JSONResponse({
            "status": "success",
            "moved": 0,
            "message": "No spam emails to move",
            "source": source
        })
    
    try:
        moved = gmail_service.move_emails_to_trash(spam_ids)
        print(f"🗑️ Moved {moved} emails to trash (source: {source})")
        return JSONResponse({
            "status": "success",
            "moved": moved,
            "message": f"{moved} mails have been moved to trash",
            "source": source
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to move emails: {str(e)}")


@app.get("/spam-summary")
async def spam_summary(request: Request):
    """Get spam count and source."""
    emails, source = _get_active_results(request)
    
    if not emails:
        return JSONResponse({
            "count": 0,
            "total": 0,
            "source": "model prediction"
        })
    
    spam_count = sum(1 for e in emails if str(e.get("prediction", "")).lower().strip() == "spam")
    
    return JSONResponse({
        "count": spam_count,
        "total": len(emails),
        "not_spam": len(emails) - spam_count,
        "source": source
    })


@app.get("/cache-stats")
async def cache_stats():
    """Get current cache statistics."""
    import sys
    cleanup_expired_cache()
    
    total_users = len(email_cache)
    total_size = sum(sys.getsizeof(json.dumps(data["emails"])) for data in email_cache.values()) if total_users > 0 else 0
    
    return JSONResponse({
        "cached_users": total_users,
        "total_memory_kb": round(total_size / 1024, 2),
        "total_memory_mb": round(total_size / (1024 * 1024), 2),
        "cache_entries": [
            {
                "user_id": uid[:8] + "...",
                "email_count": len(data["emails"]),
                "source": data["source"],
                "expires_in_minutes": round((data["expires_at"] - dt.now()).total_seconds() / 60, 1)
            }
            for uid, data in email_cache.items()
        ]
    })


@app.get("/debug-session")
async def debug_session(request: Request):
    """Debug session data."""
    return JSONResponse({
        "session_keys": list(request.session.keys()),
        "has_credentials": "credentials" in request.session,
        "has_user_id": "user_id" in request.session,
        "user_id": request.session.get("user_id", "None")[:8] + "..." if request.session.get("user_id") else "None"
    })


@app.post("/logout")
async def logout(request: Request):
    """Clear session and cache."""
    user_id = request.session.get("user_id")
    if user_id and user_id in email_cache:
        del email_cache[user_id]
        print(f"🗑️ Cleared cache for user {user_id[:8]}...")
    
    request.session.clear()
    return RedirectResponse("/", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
