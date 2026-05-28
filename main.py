from contextlib import asynccontextmanager
from typing import List
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from database import init_db, get_db
from schemas import (
    UserCreate, 
    UserResponse, 
    EventCreate, 
    EventUpdate, 
    EventResponse
)
from services import UserService, EventService
from config import settings


# Lifespan context manager to handle application startup and shutdown events.
# This replaces the deprecated @app.on_event("startup") decorator in modern FastAPI.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database table initialization (creates tables if they don't exist)
    # Excellent for quick deployments and MVP setups.
    print("[PLANIRUY-MVP] Starting up: initializing database tables...")
    try:
        await init_db()
        print("[PLANIRUY-MVP] Database tables initialized successfully.")
    except Exception as e:
        print(f"[PLANIRUY-MVP] Critical error during database initialization: {e}")
    yield
    print("[PLANIRUY-MVP] Shutting down: cleaning up resources...")


# Initialize FastAPI app with metadata and modern lifespan
app = FastAPI(
    title="планиИруй! MVP API",
    description="Asynchronous API for student academic schedule planning & stress reduction.",
    version="1.0.0",
    lifespan=lifespan
)

# Register the interactive web dashboard router
from dashboard_router import router as dashboard_router
app.include_router(dashboard_router)


# ==========================================
# USER ENDPOINTS
# ==========================================

@app.post(
    "/users/", 
    response_model=UserResponse, 
    status_code=status.HTTP_201_CREATED,
    summary="Register a new student user",
    tags=["Users"]
)
async def create_user(
    user_in: UserCreate, 
    db: AsyncSession = Depends(get_db)
):
    """
    Registers a new student user with their unique Telegram ID and timezone.
    Returns HTTP 400 if the Telegram ID is already registered.
    """
    service = UserService(db)
    try:
        return await service.create_user(user_in)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=str(e)
        )


@app.get(
    "/users/{user_id}", 
    response_model=UserResponse,
    summary="Get user details by internal ID",
    tags=["Users"]
)
async def get_user_by_id(
    user_id: int, 
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieves user details based on their internal database primary key ID.
    Returns HTTP 404 if the user does not exist.
    """
    service = UserService(db)
    user = await service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"User with ID {user_id} not found."
        )
    return user


@app.get(
    "/users/telegram/{tg_id}", 
    response_model=UserResponse,
    summary="Get user details by Telegram ID",
    tags=["Users"]
)
async def get_user_by_tg_id(
    tg_id: int, 
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieves user details based on their unique Telegram ID.
    Useful for checking registration status when a user starts the Telegram Bot.
    Returns HTTP 404 if the user is not found.
    """
    service = UserService(db)
    user = await service.get_user_by_tg_id(tg_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"User with Telegram ID {tg_id} not found."
        )
    return user


# ==========================================
# EVENT ENDPOINTS
# ==========================================

@app.post(
    "/events/", 
    response_model=EventResponse, 
    status_code=status.HTTP_201_CREATED,
    summary="Create a new event",
    tags=["Events"]
)
async def create_event(
    event_in: EventCreate, 
    db: AsyncSession = Depends(get_db)
):
    """
    Creates a new student event (e.g. study task, deadline, test).
    
    Optionally, a list of datetime objects can be supplied in `reminders`
    to automatically generate push-notification reminders for this event.
    
    Returns HTTP 400 if the corresponding user_id does not exist.
    """
    service = EventService(db)
    try:
        return await service.create_event(event_in)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=str(e)
        )


@app.get(
    "/events/{event_id}", 
    response_model=EventResponse,
    summary="Retrieve specific event details",
    tags=["Events"]
)
async def get_event(
    event_id: int, 
    db: AsyncSession = Depends(get_db)
):
    """
    Fetches details of a specific event, including all set reminders.
    Returns HTTP 404 if the event is not found.
    """
    service = EventService(db)
    event = await service.get_event(event_id)
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Event with ID {event_id} not found."
        )
    return event


@app.get(
    "/users/{user_id}/events/", 
    response_model=List[EventResponse],
    summary="Retrieve all events for a specific user",
    tags=["Events"]
)
async def get_user_events(
    user_id: int, 
    db: AsyncSession = Depends(get_db)
):
    """
    Fetches all events configured by a student, sorted by their upcoming deadline.
    Returns an empty list if no events are configured.
    """
    # Verify user exists first to keep API clean
    user_service = UserService(db)
    user = await user_service.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"User with ID {user_id} not found."
        )
        
    event_service = EventService(db)
    return await event_service.get_user_events(user_id)


@app.put(
    "/events/{event_id}", 
    response_model=EventResponse,
    summary="Update an event",
    tags=["Events"]
)
async def update_event(
    event_id: int, 
    event_in: EventUpdate, 
    db: AsyncSession = Depends(get_db)
):
    """
    Updates designated fields of an event (e.g. change status to 'confirmed', delay deadline).
    Returns HTTP 404 if the event is not found.
    """
    service = EventService(db)
    updated_event = await service.update_event(event_id, event_in)
    if not updated_event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Event with ID {event_id} not found."
        )
    return updated_event


@app.delete(
    "/events/{event_id}",
    summary="Delete an event",
    tags=["Events"]
)
async def delete_event(
    event_id: int, 
    db: AsyncSession = Depends(get_db)
):
    """
    Removes an event from the database. 
    
    All associated reminders will be automatically deleted from the database
    due to cascading foreign key constraints (`ondelete='CASCADE'`).
    
    Returns HTTP 404 if the event was not found.
    """
    service = EventService(db)
    success = await service.delete_event(event_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Event with ID {event_id} not found."
        )
    return {"status": "success", "message": f"Event {event_id} and all related reminders deleted."}


# ==========================================
# GOOGLE CALENDAR OAUTH2 ENDPOINTS
# ==========================================

@app.get(
    "/google/login", 
    summary="Initiate Google OAuth2 flow",
    tags=["Google Calendar"]
)
async def google_login(tg_id: int):
    """
    Generates Google consent authorization URL and redirects the user.
    Integrates the student's unique Telegram ID into the OAuth2 state parameter.
    """
    from google_auth_oauthlib.flow import Flow
    from fastapi.responses import RedirectResponse
    
    # Standard Web Application credentials structure
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID or "dummy-client-id",
            "client_secret": settings.GOOGLE_CLIENT_SECRET or "dummy-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    try:
        flow = Flow.from_client_config(
            client_config=client_config,
            scopes=["https://www.googleapis.com/auth/calendar"],
            redirect_uri=settings.GOOGLE_REDIRECT_URI
        )
        
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            state=str(tg_id)
        )
        return RedirectResponse(auth_url)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate Google auth URL: {e}"
        )


@app.get(
    "/google/callback", 
    summary="Google OAuth2 callback redirect endpoint",
    tags=["Google Calendar"]
)
async def google_callback(
    code: str, 
    state: str, 
    db: AsyncSession = Depends(get_db)
):
    """
    Processes the redirect code from Google, fetches OAuth2 tokens,
    and registers access/refresh credentials to the student's User record.
    """
    from google_auth_oauthlib.flow import Flow
    from fastapi.responses import HTMLResponse
    
    try:
        tg_id = int(state)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth2 state parameter (expected student Telegram ID)."
        )
        
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID or "dummy-client-id",
            "client_secret": settings.GOOGLE_CLIENT_SECRET or "dummy-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    try:
        flow = Flow.from_client_config(
            client_config=client_config,
            scopes=["https://www.googleapis.com/auth/calendar"],
            redirect_uri=settings.GOOGLE_REDIRECT_URI
        )
        
        # Exchange authorization code for tokens
        flow.fetch_token(code=code)
        credentials = flow.credentials
        
        # Fetch matching student user
        user_service = UserService(db)
        user = await user_service.get_user_by_tg_id(tg_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with Telegram ID {tg_id} not registered."
            )
            
        # Update user credentials
        user.google_access_token = credentials.token
        user.google_refresh_token = credentials.refresh_token
        user.google_token_expiry = credentials.expiry
        
        await db.commit()
        
        # Premium styled HTML Response
        html_content = """
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>планиИруй! — Календарь Подключен</title>
            <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
            <style>
                * {
                    box-sizing: border-box;
                    margin: 0;
                    padding: 0;
                }
                body {
                    font-family: 'Inter', sans-serif;
                    background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
                    color: #f8fafc;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    min-height: 100vh;
                    overflow: hidden;
                }
                .container {
                    background: rgba(30, 41, 59, 0.7);
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    backdrop-filter: blur(16px);
                    padding: 48px;
                    border-radius: 24px;
                    text-align: center;
                    max-width: 520px;
                    width: 90%;
                    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                    animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1);
                }
                @keyframes slideUp {
                    from { opacity: 0; transform: translateY(20px); }
                    to { opacity: 1; transform: translateY(0); }
                }
                .logo {
                    font-family: 'Outfit', sans-serif;
                    font-size: 36px;
                    font-weight: 800;
                    background: linear-gradient(135deg, #38bdf8 0%, #818cf8 100%);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    margin-bottom: 24px;
                }
                .success-badge {
                    width: 72px;
                    height: 72px;
                    background: rgba(16, 185, 129, 0.15);
                    color: #10b981;
                    font-size: 32px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 50%;
                    margin: 0 auto 24px;
                    border: 1px solid rgba(16, 185, 129, 0.3);
                    box-shadow: 0 0 20px rgba(16, 185, 129, 0.2);
                    animation: pulse 2s infinite;
                }
                @keyframes pulse {
                    0% { transform: scale(1); }
                    50% { transform: scale(1.05); }
                    100% { transform: scale(1); }
                }
                h1 {
                    font-family: 'Outfit', sans-serif;
                    font-size: 24px;
                    font-weight: 600;
                    margin-bottom: 12px;
                    color: #ffffff;
                }
                p {
                    color: #94a3b8;
                    font-size: 15px;
                    line-height: 1.6;
                    margin-bottom: 32px;
                }
                .btn {
                    display: inline-block;
                    background: linear-gradient(135deg, #0ea5e9 0%, #6366f1 100%);
                    color: white;
                    font-weight: 600;
                    text-decoration: none;
                    padding: 14px 28px;
                    border-radius: 12px;
                    box-shadow: 0 4px 15px rgba(99, 102, 241, 0.3);
                    transition: all 0.2s ease;
                }
                .btn:hover {
                    transform: translateY(-2px);
                    box-shadow: 0 6px 20px rgba(99, 102, 241, 0.4);
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="logo">планиИруй!</div>
                <div class="success-badge">✓</div>
                <h1>Google Календарь подключен!</h1>
                <p>Ваш аккаунт успешно синхронизирован. Теперь все подтвержденные задачи будут автоматически планироваться в вашем Google Календаре в режиме реального времени.</p>
                <a href="https://t.me/" class="btn">Вернуться в Telegram</a>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content, status_code=status.HTTP_200_OK)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google OAuth2 authentication failed: {e}"
        )
