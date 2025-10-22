from fastapi import FastAPI, APIRouter, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Optional
import uuid
from datetime import datetime, timezone
import json
import asyncio

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.waiting_queue: List[str] = []
        self.chat_pairs: Dict[str, str] = {}  # user_id -> partner_id
        self.user_info: Dict[str, dict] = {}  # user_id -> {username, session_id}
        self.typing_status: Dict[str, bool] = {}  # user_id -> is_typing

    async def connect(self, websocket: WebSocket, user_id: str, username: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        self.user_info[user_id] = {
            "username": username,
            "session_id": str(uuid.uuid4())
        }
        self.typing_status[user_id] = False
        logger.info(f"User {username} ({user_id}) connected")

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
        if user_id in self.waiting_queue:
            self.waiting_queue.remove(user_id)
        if user_id in self.user_info:
            del self.user_info[user_id]
        if user_id in self.typing_status:
            del self.typing_status[user_id]
        logger.info(f"User {user_id} disconnected")

    async def find_match(self, user_id: str):
        # Add user to waiting queue if not already there
        if user_id not in self.waiting_queue:
            self.waiting_queue.append(user_id)
        
        # Try to find a match
        if len(self.waiting_queue) >= 2:
            user1_id = self.waiting_queue.pop(0)
            user2_id = self.waiting_queue.pop(0)
            
            # Create chat pair
            self.chat_pairs[user1_id] = user2_id
            self.chat_pairs[user2_id] = user1_id
            
            # Create session in database
            session_id = str(uuid.uuid4())
            session_doc = {
                "session_id": session_id,
                "user1_id": user1_id,
                "user1_username": self.user_info[user1_id]["username"],
                "user2_id": user2_id,
                "user2_username": self.user_info[user2_id]["username"],
                "started_at": datetime.now(timezone.utc).isoformat(),
                "ended_at": None,
                "messages": []
            }
            await db.chat_sessions.insert_one(session_doc)
            
            # Notify both users
            await self.send_personal_message(user1_id, {
                "type": "matched",
                "partner_username": self.user_info[user2_id]["username"],
                "session_id": session_id
            })
            await self.send_personal_message(user2_id, {
                "type": "matched",
                "partner_username": self.user_info[user1_id]["username"],
                "session_id": session_id
            })
            
            return True
        return False

    async def disconnect_pair(self, user_id: str):
        if user_id in self.chat_pairs:
            partner_id = self.chat_pairs[user_id]
            
            # End session in database
            await db.chat_sessions.update_one(
                {
                    "$or": [
                        {"user1_id": user_id, "user2_id": partner_id},
                        {"user1_id": partner_id, "user2_id": user_id}
                    ],
                    "ended_at": None
                },
                {"$set": {"ended_at": datetime.now(timezone.utc).isoformat()}}
            )
            
            # Notify partner
            if partner_id in self.active_connections:
                await self.send_personal_message(partner_id, {
                    "type": "partner_disconnected"
                })
            
            # Remove pair
            del self.chat_pairs[user_id]
            if partner_id in self.chat_pairs:
                del self.chat_pairs[partner_id]

    async def send_personal_message(self, user_id: str, message: dict):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except Exception as e:
                logger.error(f"Error sending message to {user_id}: {e}")

    async def send_message_to_pair(self, sender_id: str, message: dict):
        if sender_id in self.chat_pairs:
            partner_id = self.chat_pairs[sender_id]
            
            # Store message in database
            message_doc = {
                "message_id": str(uuid.uuid4()),
                "sender_id": sender_id,
                "sender_username": self.user_info[sender_id]["username"],
                "message": message.get("message", ""),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            # Find and update the session
            await db.chat_sessions.update_one(
                {
                    "$or": [
                        {"user1_id": sender_id, "user2_id": partner_id},
                        {"user1_id": partner_id, "user2_id": sender_id}
                    ],
                    "ended_at": None
                },
                {"$push": {"messages": message_doc}}
            )
            
            # Send to partner
            await self.send_personal_message(partner_id, {
                "type": "message",
                "username": self.user_info[sender_id]["username"],
                "message": message.get("message", ""),
                "timestamp": message_doc["timestamp"]
            })

    async def send_typing_status(self, user_id: str, is_typing: bool):
        if user_id in self.chat_pairs:
            partner_id = self.chat_pairs[user_id]
            self.typing_status[user_id] = is_typing
            await self.send_personal_message(partner_id, {
                "type": "typing",
                "is_typing": is_typing
            })

manager = ConnectionManager()

# WebSocket endpoint
@app.websocket("/api/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    username = websocket.query_params.get("username", "Anonymous")
    await manager.connect(websocket, user_id, username)
    
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message["type"] == "find_match":
                await manager.find_match(user_id)
            
            elif message["type"] == "message":
                await manager.send_message_to_pair(user_id, message)
            
            elif message["type"] == "typing":
                await manager.send_typing_status(user_id, message.get("is_typing", False))
            
            elif message["type"] == "skip":
                await manager.disconnect_pair(user_id)
                await manager.find_match(user_id)
            
            elif message["type"] == "disconnect":
                await manager.disconnect_pair(user_id)
                break
    
    except WebSocketDisconnect:
        await manager.disconnect_pair(user_id)
        manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"WebSocket error for user {user_id}: {e}")
        await manager.disconnect_pair(user_id)
        manager.disconnect(user_id)

# Define Models
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

class ChatSession(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: str
    user1_username: str
    user2_username: str
    started_at: str
    ended_at: Optional[str]
    message_count: int

# API routes
@api_router.get("/")
async def root():
    return {"message": "Random Chat API"}

@api_router.get("/stats")
async def get_stats():
    total_sessions = await db.chat_sessions.count_documents({})
    active_sessions = await db.chat_sessions.count_documents({"ended_at": None})
    online_users = len(manager.active_connections)
    
    return {
        "total_sessions": total_sessions,
        "active_sessions": active_sessions,
        "online_users": online_users,
        "waiting_queue": len(manager.waiting_queue)
    }

@api_router.get("/sessions", response_model=List[ChatSession])
async def get_chat_sessions(limit: int = 50):
    sessions = await db.chat_sessions.find(
        {},
        {"_id": 0}
    ).sort("started_at", -1).limit(limit).to_list(limit)
    
    # Add message count
    for session in sessions:
        session["message_count"] = len(session.get("messages", []))
        # Remove messages from response for brevity
        session.pop("messages", None)
        session.pop("user1_id", None)
        session.pop("user2_id", None)
    
    return sessions

@api_router.get("/session/{session_id}")
async def get_session_details(session_id: str):
    session = await db.chat_sessions.find_one(
        {"session_id": session_id},
        {"_id": 0}
    )
    
    if not session:
        return {"error": "Session not found"}
    
    return session

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()