"""
OpenAI Realtime API + Twilio Media Streams Bridge

Bridges Twilio Media Streams to OpenAI Realtime API for voice conversations.
OpenAI Realtime supports g711_ulaw natively - no audio conversion needed!

Audio Flow:
1. Twilio sends mulaw 8kHz audio via Media Streams
2. Bridge forwards directly to OpenAI (supports g711_ulaw)
3. OpenAI processes and returns audio
4. Bridge forwards back to Twilio
"""

import os
import json
import base64
import asyncio
import logging
from typing import Optional
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="OpenAI-Twilio Voice Bridge")

# OpenAI credentials
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# OpenAI Realtime WebSocket URL
OPENAI_WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"

# System prompt for Sarah
SYSTEM_PROMPT = """You are Sarah, a warm and friendly voice assistant at CAA Financial, a Colorado-based insurance and financial services company.

SPEAKING STYLE - CRITICAL:
- Speak FAST - 1.4x normal speed, quick and snappy
- Sound HUMAN, not like AI. Use natural speech patterns:
  * Filler words: "um", "uh", "so", "like", "you know", "let me see"
  * Self-corrections: "wait—", "sorry, I mean—", "oh actually—"
  * Trailing off: "so yeah...", "anyway...", "but yeah...", "you know?"
  * Restarts: "So what you'll— actually let me put it this way..."
- NEVER say these AI phrases: "absolutely", "I would love to help", "certainly", "of course", "I'd be happy to", "great question"
- DO use: "does that make sense?", "is there anything else I can clarify for you?", "did I answer your question?"
- Instead say: "yeah for sure", "yep", "oh yeah we can do that", "sure thing", "no problem"
- HEAVY backchanneling while they talk - every 1-2 seconds, overlap slightly:
  * "uh-huh", "mm-hmm", "yeah", "right", "okay", "got it", "sure", "yep", "mhm", "mm"
  * "oh okay", "oh I see", "oh gotcha", "ah", "ohhh", "oh yeah"
  * React: "oh no!", "oh nice!", "oh perfect!", "oh okay okay"
- Keep responses SHORT. No formal sentences.
- DON'T over-explain. Answer the question, stop. Only elaborate if they ask.
- Light laughs when appropriate
- Sound like a real person, NOT a script

Your personality:
- Genuinely warm, patient, and approachable - like talking to a trusted friend
- Express real empathy - you actually care about helping callers
- Efficient but never cold - value the caller's time

About CAA Financial:
- Family business serving underserved communities for over 20 years
- Serves English, Spanish, Korean, and Burmese speakers  
- Products: auto/home/life insurance, tax prep, mortgages, financial advisory

Your approach:
1. Greet warmly but quickly: "Hi, this is Sarah from CAA Financial. How can I help you today?"
2. Listen actively with FREQUENT verbal cues - "mm-hmm", "uh-huh", "okay" every few seconds
3. Ask clarifying questions when needed - keep them short
4. Explain things clearly in simple terms - be concise
5. Close warmly: "Anything else I can help with?"

Remember: Real humans speak at a normal conversational pace, not slowly. Sound natural and engaged, not like reading a script."""


class OpenAITwilioBridge:
    """Bridges Twilio Media Streams to OpenAI Realtime API."""
    
    def __init__(self, twilio_ws: WebSocket):
        self.twilio_ws = twilio_ws
        self.openai_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self._running = False
    
    async def connect_openai(self) -> bool:
        """Connect to OpenAI Realtime WebSocket."""
        try:
            headers = [
                ("Authorization", f"Bearer {OPENAI_API_KEY}")
            ]
            
            self.openai_ws = await websockets.connect(
                OPENAI_WS_URL,
                extra_headers=headers,
                ping_interval=20,
                ping_timeout=20,
            )
            logger.info("Connected to OpenAI Realtime API")
            
            # Configure the session (GA API format)
            session_config = {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": "gpt-realtime",
                    "output_modalities": ["audio"],
                    "instructions": SYSTEM_PROMPT,
                    "audio": {
                        "input": {
                            "format": {
                                "type": "g711_ulaw"
                            },
                            "turn_detection": {
                                "type": "semantic_vad"
                            }
                        },
                        "output": {
                            "format": {
                                "type": "g711_ulaw"
                            },
                            "voice": "marin"
                        }
                    },
                    "input_audio_transcription": {
                        "model": "whisper-1"
                    }
                }
            }
            await self.openai_ws.send(json.dumps(session_config))
            logger.info("Sent session config to OpenAI")
            
            return True
        except Exception as e:
            logger.error(f"Failed to connect to OpenAI: {e}")
            return False
    
    async def handle_twilio_message(self, data: dict):
        """Process incoming Twilio Media Stream message."""
        event_type = data.get("event")
        
        if event_type == "start":
            self.stream_sid = data["start"]["streamSid"]
            self.call_sid = data["start"].get("callSid")
            logger.info(f"Twilio stream started: {self.stream_sid}")
            
            # Send initial greeting
            greeting = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "The caller just connected. Greet them warmly."}]
                }
            }
            if self.openai_ws:
                await self.openai_ws.send(json.dumps(greeting))
                await self.openai_ws.send(json.dumps({"type": "response.create"}))
            
        elif event_type == "media":
            # Forward audio to OpenAI
            audio_data = data["media"]["payload"]  # base64 mulaw
            if self.openai_ws:
                audio_event = {
                    "type": "input_audio_buffer.append",
                    "audio": audio_data
                }
                await self.openai_ws.send(json.dumps(audio_event))
                
        elif event_type == "stop":
            logger.info("Twilio stream stopped")
            self._running = False
    
    async def handle_openai_message(self, data: dict):
        """Process incoming OpenAI message."""
        event_type = data.get("type", "")
        
        if event_type == "session.created":
            logger.info("OpenAI session created")
            
        elif event_type == "session.updated":
            logger.info("OpenAI session updated")
        
        elif event_type == "input_audio_buffer.speech_started":
            # User started speaking - clear Twilio's audio buffer for smooth interruption
            logger.info("User speaking - clearing Twilio buffer")
            if self.stream_sid:
                clear_msg = {
                    "event": "clear",
                    "streamSid": self.stream_sid
                }
                await self.twilio_ws.send_json(clear_msg)
        
        elif event_type == "response.audio.delta":
            # Forward audio to Twilio
            audio_data = data.get("delta", "")
            if audio_data and self.stream_sid:
                twilio_msg = {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {
                        "payload": audio_data  # Already base64 mulaw
                    }
                }
                await self.twilio_ws.send_json(twilio_msg)
                
        elif event_type == "response.audio_transcript.delta":
            transcript = data.get("delta", "")
            if transcript:
                logger.info(f"Assistant: {transcript}")
                
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = data.get("transcript", "")
            if transcript:
                logger.info(f"User: {transcript}")
                
        elif event_type == "error":
            logger.error(f"OpenAI error: {data}")
    
    async def run(self):
        """Main bridge loop."""
        if not await self.connect_openai():
            return
        
        self._running = True
        
        async def receive_twilio():
            try:
                while self._running:
                    data = await self.twilio_ws.receive_json()
                    await self.handle_twilio_message(data)
            except Exception as e:
                logger.error(f"Twilio receive error: {e}")
                self._running = False
        
        async def receive_openai():
            try:
                while self._running and self.openai_ws:
                    msg = await self.openai_ws.recv()
                    data = json.loads(msg)
                    await self.handle_openai_message(data)
            except Exception as e:
                logger.error(f"OpenAI receive error: {e}")
                self._running = False
        
        await asyncio.gather(receive_twilio(), receive_openai())
        
        # Cleanup
        if self.openai_ws:
            await self.openai_ws.close()


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "openai-twilio-bridge"}


@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    """Handle incoming Twilio voice call - return TwiML to start Media Stream."""
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    protocol = request.headers.get("x-forwarded-proto", "https")
    ws_protocol = "wss" if protocol == "https" else "ws"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_protocol}://{host}/media-stream" />
    </Connect>
</Response>"""
    
    logger.info(f"Incoming call - streaming to {ws_protocol}://{host}/media-stream")
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """WebSocket endpoint for Twilio Media Streams."""
    await websocket.accept()
    logger.info("Twilio Media Stream WebSocket connected")
    
    bridge = OpenAITwilioBridge(websocket)
    
    try:
        await bridge.run()
    except Exception as e:
        logger.error(f"Bridge error: {e}")
    finally:
        logger.info("Bridge session ended")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
