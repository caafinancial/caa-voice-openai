"""
OpenAI Realtime API + Twilio Media Streams Bridge
with MongoDB integration for policy lookups.

Audio Flow:
1. Twilio sends mulaw 8kHz audio via Media Streams
2. Bridge forwards directly to OpenAI (supports g711_ulaw)
3. OpenAI processes and returns audio
4. Bridge forwards back to Twilio

Function Calling:
- OpenAI can call tools to lookup customer/policy data from MongoDB
- Caller identified by phone number from Twilio
"""

import os
import json
import base64
import asyncio
import logging
import struct
from typing import Optional, Dict, Any
import numpy as np
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import Response
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============ CALL CENTER BACKGROUND NOISE ============
# Adds subtle ambient office/call center murmur to make Sarah sound more real

BACKGROUND_NOISE_VOLUME = 0.10  # 10% volume - busy office presence

# Mulaw encoding/decoding tables
MULAW_BIAS = 33
MULAW_MAX = 32635

def linear_to_mulaw(sample: int) -> int:
    """Convert 16-bit linear PCM sample to 8-bit mulaw."""
    sign = (sample >> 8) & 0x80
    if sign:
        sample = -sample
    sample = min(sample + MULAW_BIAS, MULAW_MAX)
    
    # Find segment and quantization
    exponent = 7
    for i in range(7, 0, -1):
        if sample >= (1 << (i + 3)):
            exponent = i
            break
    else:
        exponent = 0
    
    mantissa = (sample >> (exponent + 3)) & 0x0F
    mulaw_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return mulaw_byte

def mulaw_to_linear(mulaw_byte: int) -> int:
    """Convert 8-bit mulaw to 16-bit linear PCM sample."""
    mulaw_byte = ~mulaw_byte & 0xFF
    sign = mulaw_byte & 0x80
    exponent = (mulaw_byte >> 4) & 0x07
    mantissa = mulaw_byte & 0x0F
    
    sample = ((mantissa << 3) + MULAW_BIAS) << exponent
    sample -= MULAW_BIAS
    
    if sign:
        sample = -sample
    return sample

class BackgroundNoiseGenerator:
    """Generates subtle call center background ambience."""
    
    def __init__(self, sample_rate: int = 8000):
        self.sample_rate = sample_rate
        self.position = 0
        # Pre-generate a few seconds of ambient noise (looped)
        self.noise_buffer = self._generate_ambient_noise(duration_sec=3.0)
        
    def _generate_ambient_noise(self, duration_sec: float) -> bytes:
        """Generate busy office ambient noise (typing, murmur, activity)."""
        num_samples = int(self.sample_rate * duration_sec)
        
        # Create layered ambient noise
        t = np.linspace(0, duration_sec, num_samples)
        
        # Very subtle room tone (less rumble, more natural)
        room_tone = np.sin(2 * np.pi * 100 * t) * 0.03
        
        # Pink noise for "busy office murmur" (distant conversations)
        white = np.random.randn(num_samples)
        # Simple pink noise approximation
        pink = np.zeros(num_samples)
        b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
        a = [1, -2.494956002, 2.017265875, -0.522189400]
        for i in range(3, num_samples):
            pink[i] = (b[0]*white[i] + b[1]*white[i-1] + b[2]*white[i-2] + b[3]*white[i-3]
                      - a[1]*pink[i-1] - a[2]*pink[i-2] - a[3]*pink[i-3])
        pink = pink / (np.max(np.abs(pink)) + 0.001) * 0.12
        
        # More keyboard typing sounds (busy office!)
        clicks = np.zeros(num_samples)
        # ~8-12 clicks per second for busy typing
        click_positions = np.random.choice(num_samples, size=int(duration_sec * 10), replace=False)
        for pos in click_positions:
            click_len = min(40, num_samples - pos)
            # Sharper click sound
            clicks[pos:pos+click_len] = np.random.randn(click_len) * 0.04 * np.exp(-np.linspace(0, 8, click_len))
        
        # Occasional distant phone ring (very subtle, every ~2 sec)
        phone_positions = np.random.choice(num_samples, size=int(duration_sec * 0.5), replace=False)
        phones = np.zeros(num_samples)
        for pos in phone_positions:
            ring_len = min(800, num_samples - pos)  # ~100ms ring
            ring_t = np.linspace(0, ring_len/self.sample_rate, ring_len)
            # Dual-tone phone ring (classic office phone)
            phones[pos:pos+ring_len] = (np.sin(2 * np.pi * 440 * ring_t) + 
                                        np.sin(2 * np.pi * 480 * ring_t)) * 0.015 * np.exp(-ring_t * 8)
        
        # Mix layers - more emphasis on activity sounds
        ambient = room_tone + pink + clicks + phones
        
        # Normalize and scale
        ambient = ambient / (np.max(np.abs(ambient)) + 0.001) * 0.5
        
        # Convert to 16-bit PCM then to mulaw
        pcm_samples = (ambient * 16000).astype(np.int16)
        mulaw_bytes = bytes([linear_to_mulaw(int(s)) for s in pcm_samples])
        
        return mulaw_bytes
    
    def get_noise_chunk(self, num_samples: int) -> bytes:
        """Get a chunk of background noise (loops automatically)."""
        result = bytearray(num_samples)
        for i in range(num_samples):
            result[i] = self.noise_buffer[self.position]
            self.position = (self.position + 1) % len(self.noise_buffer)
        return bytes(result)

def mix_audio_with_background(audio_b64: str, noise_generator: BackgroundNoiseGenerator, volume: float = BACKGROUND_NOISE_VOLUME) -> str:
    """Mix voice audio with background noise."""
    try:
        # Decode base64 mulaw audio
        audio_bytes = base64.b64decode(audio_b64)
        num_samples = len(audio_bytes)
        
        # Get matching noise chunk
        noise_bytes = noise_generator.get_noise_chunk(num_samples)
        
        # Mix: convert to linear, mix, convert back to mulaw
        mixed = bytearray(num_samples)
        for i in range(num_samples):
            # Convert both to linear PCM
            voice_linear = mulaw_to_linear(audio_bytes[i])
            noise_linear = mulaw_to_linear(noise_bytes[i])
            
            # Mix with noise at reduced volume
            mixed_linear = int(voice_linear + noise_linear * volume)
            
            # Clamp to valid range
            mixed_linear = max(-32768, min(32767, mixed_linear))
            
            # Convert back to mulaw
            mixed[i] = linear_to_mulaw(mixed_linear)
        
        # Encode back to base64
        return base64.b64encode(bytes(mixed)).decode('utf-8')
    except Exception as e:
        logger.error(f"Audio mixing error: {e}")
        return audio_b64  # Return original on error


# Global noise generator
background_noise = BackgroundNoiseGenerator()
# ============ END BACKGROUND NOISE ============

app = FastAPI(title="OpenAI-Twilio Voice Bridge")

# Credentials from environment
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MONGO_URI = os.environ.get("MONGO_URI", "")

# MongoDB client (initialized on startup)
mongo_client: Optional[AsyncIOMotorClient] = None
db = None

# OpenAI Realtime WebSocket URL
OPENAI_WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"

# Tools for function calling
TOOLS = [
    {
        "type": "function",
        "name": "lookup_customer",
        "description": "ALWAYS use this first when caller gives their name or phone number. Look up customer by name (e.g. 'Lawrence Choi') or phone number. Call this immediately when someone identifies themselves.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Phone number the caller provided (any format)"
                },
                "name": {
                    "type": "string",
                    "description": "Name the caller provided (e.g. 'Lawrence Choi', 'John Smith')"
                }
            },
            "required": []
        }
    },
    {
        "type": "function",
        "name": "lookup_policy",
        "description": "Get policy details (coverage, deductibles, premiums). Use AFTER finding the customer. Searches by the customer's phone number.",
        "parameters": {
            "type": "object",
            "properties": {
                "policy_number": {
                    "type": "string",
                    "description": "The policy number if known"
                },
                "policy_type": {
                    "type": "string",
                    "enum": ["auto", "home", "life", "any"],
                    "description": "Type of policy to look up"
                }
            },
            "required": []
        }
    },
    {
        "type": "function",
        "name": "get_account_summary",
        "description": "Get a quick summary of the customer's account - active policies, total premium, pending tasks. Use for general account questions.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

# System prompt for Sarah
SYSTEM_PROMPT = """You are Sarah, a friendly voice assistant at CAA Financial.

CRITICAL - ALWAYS USE TOOLS:
- When caller says their NAME → immediately call lookup_customer with their name
- When caller says their PHONE → immediately call lookup_customer with their phone
- When they ask about policy/coverage → call lookup_policy
- Say "let me look that up" while you call the tool

HOW TO SPEAK:
- Warm and conversational, like a helpful coworker
- Keep responses SHORT
- Natural acknowledgments: "okay", "got it", "sure"

AVOID:
- AI phrases: "absolutely", "certainly", "I'd be happy to"
- Long explanations

ABOUT CAA FINANCIAL:
- Family business, 20+ years
- English, Spanish, Korean, Burmese
- Insurance, tax prep, mortgages"""


@app.on_event("startup")
async def startup():
    """Initialize MongoDB connection on startup."""
    global mongo_client, db
    if MONGO_URI:
        try:
            mongo_client = AsyncIOMotorClient(MONGO_URI)
            # Use explicit database name since URI might not have one
            db = mongo_client["caafinancial"]
            # Test connection
            await db.command("ping")
            logger.info(f"Connected to MongoDB: {db.name}")
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}")
    else:
        logger.warning("MONGO_URI not set - database lookups will return mock data")


@app.on_event("shutdown")
async def shutdown():
    """Close MongoDB connection on shutdown."""
    global mongo_client
    if mongo_client:
        mongo_client.close()


async def execute_function(name: str, args: Dict[str, Any], caller_phone: str) -> str:
    """Execute a function call and return the result as a string."""
    logger.info(f"Executing function: {name} with args: {args}, caller: {caller_phone}")
    
    # Use phone from args if provided, otherwise use caller_phone
    phone_to_use = args.get("phone") or caller_phone
    
    # Normalize phone number (remove +1, spaces, dashes)
    normalized_phone = phone_to_use.replace("+1", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    
    # Create a flexible regex pattern that matches phone with any formatting
    # e.g., "3033177032" becomes "3.*0.*3.*3.*1.*7.*7.*0.*3.*2" to match "(303) 317-7032"
    phone_pattern = ".*".join(list(normalized_phone)) if normalized_phone and normalized_phone != "unknown" else None
    logger.info(f"Phone lookup pattern: {phone_pattern} (from {phone_to_use})")
    
    if db is None:
        # Return mock data if no database
        return json.dumps({
            "status": "mock_data",
            "message": "Database not connected. This is sample data.",
            "data": {
                "customer_name": "John Smith",
                "policy_number": "POL-12345",
                "policy_type": "auto",
                "deductible": "$500",
                "monthly_premium": "$125",
                "next_payment_due": "March 1, 2026"
            }
        })
    
    try:
        if name == "lookup_customer":
            # Look up customer by phone or name
            customer = None
            
            # Try phone lookup first if we have a pattern
            if phone_pattern:
                customer = await db.customers.find_one({
                    "$or": [
                        {"phone": {"$regex": phone_pattern, "$options": "i"}},
                        {"mobile": {"$regex": phone_pattern, "$options": "i"}}
                    ]
                })
            
            # Try by name if phone didn't work and name was provided
            if not customer and args.get("name"):
                name_query = args.get("name")
                customer = await db.customers.find_one({
                    "name": {"$regex": name_query, "$options": "i"}
                })
            if customer:
                # Remove sensitive fields
                customer.pop("_id", None)
                customer.pop("ssn", None)
                customer.pop("password", None)
                return json.dumps({"status": "found", "customer": customer})
            return json.dumps({"status": "not_found", "message": "No customer found with this phone number"})
            
        elif name == "lookup_policy":
            # First find customer using flexible phone pattern
            if not phone_pattern:
                return json.dumps({"status": "error", "message": "No phone number available for lookup"})
            
            customer = await db.customers.find_one({
                "$or": [
                    {"phone": {"$regex": phone_pattern, "$options": "i"}},
                    {"mobile": {"$regex": phone_pattern, "$options": "i"}}
                ]
            })
            if not customer:
                return json.dumps({"status": "not_found", "message": "Customer not found"})
            
            customer_id = str(customer.get("_id"))  # Convert to string - policies store customer_id as string
            
            # Look up policies
            query = {"customer_id": customer_id}
            policy_type = args.get("policy_type", "any")
            if policy_type and policy_type != "any":
                query["type"] = policy_type
            if args.get("policy_number"):
                query["policy_number"] = args["policy_number"]
                
            policies = await db.policies.find(query).to_list(10)
            
            # Enrich with policy_details (has coverage/deductible info)
            enriched = []
            for p in policies:
                policy_id = str(p["_id"])
                p["_id"] = policy_id
                p.pop("customer_id", None)
                
                # Try to get detailed coverage info
                details = await db.policy_details.find_one({"policy_id": policy_id})
                if details and details.get("policy_data"):
                    pd = details["policy_data"]
                    p["coverage_details"] = {
                        "total_premium": pd.get("total_premium"),
                        "current_premium": pd.get("current_premium"),
                        "policy_period": pd.get("policy_period"),
                        "bodily_injury": pd.get("policy_level_coverages", {}).get("bodily_injury", {}).get("limit"),
                        "property_damage": pd.get("policy_level_coverages", {}).get("property_damage", {}).get("limit"),
                        "uninsured_motorist": pd.get("policy_level_coverages", {}).get("uninsured_motorist", {}).get("limit"),
                        "comprehensive_deductible": pd.get("vehicle_coverages", [{}])[0].get("comprehensive", {}).get("deductible") if pd.get("vehicle_coverages") else None,
                        "collision_deductible": pd.get("vehicle_coverages", [{}])[0].get("collision", {}).get("deductible") if pd.get("vehicle_coverages") else None,
                    }
                enriched.append(p)
            
            if enriched:
                return json.dumps({"status": "found", "policies": enriched}, default=str)
            return json.dumps({"status": "not_found", "message": "No policies found"})
            
        elif name == "get_account_summary":
            # Use pre-computed customer_summaries for fast lookup
            if not phone_pattern:
                return json.dumps({"status": "error", "message": "No phone number available for lookup"})
            
            customer = await db.customers.find_one({
                "$or": [
                    {"phone": {"$regex": phone_pattern, "$options": "i"}},
                    {"mobile": {"$regex": phone_pattern, "$options": "i"}}
                ]
            })
            if not customer:
                return json.dumps({"status": "not_found", "message": "Customer not found"})
            
            customer_id = str(customer.get("_id"))  # Convert to string
            
            # Try customer_summaries first (fast, pre-computed)
            summary = await db.customer_summaries.find_one({"customer_id": customer_id})
            if summary:
                summary.pop("_id", None)
                return json.dumps({"status": "found", "summary": summary})
            
            # Fallback: compute summary
            active_policies = await db.policies.find(
                {"customer_id": customer_id, "status": "active"}
            ).to_list(20)
            
            total_premium = sum(float(p.get("premium", 0) or 0) for p in active_policies)
            
            result = {
                "status": "found",
                "customer_name": customer.get("name"),
                "active_policies": len(active_policies),
                "total_annual_premium": f"${total_premium:.2f}",
                "policy_types": list(set(p.get("type", "Unknown") for p in active_policies))
            }
            return json.dumps(result)
            
        else:
            return json.dumps({"error": f"Unknown function: {name}"})
            
    except Exception as e:
        logger.error(f"Function execution error: {e}")
        return json.dumps({"error": str(e)})


class OpenAITwilioBridge:
    """Bridges Twilio Media Streams to OpenAI Realtime API."""
    
    def __init__(self, twilio_ws: WebSocket):
        self.twilio_ws = twilio_ws
        self.openai_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.caller_phone: Optional[str] = None
        self._running = False
        self._user_interrupted = False
        self._response_active = False  # Track if OpenAI is currently generating
    
    async def connect_openai(self) -> bool:
        """Connect to OpenAI Realtime WebSocket."""
        try:
            headers = [
                ("Authorization", f"Bearer {OPENAI_API_KEY}"),
                ("OpenAI-Beta", "realtime=v1")
            ]
            
            self.openai_ws = await websockets.connect(
                OPENAI_WS_URL,
                extra_headers=headers,
                ping_interval=20,
                ping_timeout=20,
            )
            logger.info("Connected to OpenAI Realtime API")
            
            # Configure the session with tools
            session_config = {
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "instructions": SYSTEM_PROMPT,
                    "voice": "sage",
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "input_audio_transcription": {
                        "model": "whisper-1"
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.4,
                        "prefix_padding_ms": 200,
                        "silence_duration_ms": 500,
                        "create_response": True
                    },
                    "tools": TOOLS,
                    "tool_choice": "auto"
                }
            }
            await self.openai_ws.send(json.dumps(session_config))
            logger.info("Sent session config with tools to OpenAI")
            
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
            # Extract caller's phone number - try multiple sources
            custom_params = data["start"].get("customParameters", {})
            logger.info(f"Stream start data: customParams={custom_params}, full_start={data['start']}")
            
            # Try custom params first (lowercase and uppercase variants)
            self.caller_phone = (
                custom_params.get("from") or 
                custom_params.get("From") or 
                custom_params.get("caller") or
                data["start"].get("from") or
                "unknown"
            )
            logger.info(f"Twilio stream started: {self.stream_sid}, caller: {self.caller_phone}")
            
            # Send initial greeting with caller context
            greeting = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text", 
                        "text": f"A caller just connected from phone number {self.caller_phone}. Greet them warmly. You can use your tools to look up their account information if they ask about their policy."
                    }]
                }
            }
            if self.openai_ws:
                await self.openai_ws.send(json.dumps(greeting))
                await self.openai_ws.send(json.dumps({"type": "response.create"}))
            
        elif event_type == "media":
            audio_data = data["media"]["payload"]
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
            self._user_interrupted = True
            
            # Only cancel if there's actually an active response
            # Clear Twilio buffer regardless to stop any in-flight audio
            if self.stream_sid:
                clear_msg = {
                    "event": "clear",
                    "streamSid": self.stream_sid
                }
                await self.twilio_ws.send_json(clear_msg)
            
            if self._response_active and self.openai_ws:
                logger.info("User interrupted - canceling active response")
                try:
                    await self.openai_ws.send(json.dumps({"type": "response.cancel"}))
                except Exception as e:
                    logger.warning(f"Failed to send cancel: {e}")
            else:
                logger.debug("User speaking (no active response)")
        
        elif event_type == "input_audio_buffer.speech_stopped":
            logger.info("User stopped speaking")
            
        elif event_type == "response.created":
            self._user_interrupted = False
            self._response_active = True
            logger.info("Response started")
        
        elif event_type in ("response.done", "response.cancelled", "response.audio.done", "response.output_audio.done"):
            self._response_active = False
            if event_type in ("response.done", "response.cancelled"):
                logger.info(f"Response ended: {event_type}")
            
        elif event_type in ("response.audio.delta", "response.output_audio.delta"):
            # Mark that we're actively sending audio
            self._response_active = True
            
            if self._user_interrupted:
                return
            audio_data = data.get("delta", "")
            if audio_data and self.stream_sid:
                twilio_msg = {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {
                        "payload": audio_data
                    }
                }
                await self.twilio_ws.send_json(twilio_msg)
        
        elif event_type == "response.function_call_arguments.done":
            # Function call completed - execute it
            call_id = data.get("call_id")
            name = data.get("name")
            arguments = data.get("arguments", "{}")
            
            logger.info(f"Function call: {name}({arguments})")
            
            try:
                args = json.loads(arguments)
            except:
                args = {}
            
            # Execute the function
            result = await execute_function(name, args, self.caller_phone or "unknown")
            
            # Send the result back to OpenAI
            logger.info(f"Function result: {result[:200]}...")
            function_output = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result
                }
            }
            await self.openai_ws.send(json.dumps(function_output))
            logger.info("Sent function output, triggering response...")
            
            # Trigger response generation with the function result
            await self.openai_ws.send(json.dumps({"type": "response.create"}))
                
        elif event_type in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
            transcript = data.get("delta", "")
            if transcript:
                logger.info(f"Assistant: {transcript}")
                
        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = data.get("transcript", "")
            if transcript:
                logger.info(f"User: {transcript}")
                
        elif event_type == "error":
            error_info = data.get("error", {})
            error_code = error_info.get("code", "")
            # Suppress harmless cancel errors (race condition when user speaks during response end)
            if error_code == "response_cancel_not_active":
                logger.debug(f"Cancel race condition (harmless): {error_info.get('message')}")
            else:
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
        
        if self.openai_ws:
            await self.openai_ws.close()


@app.get("/health")
async def health():
    """Health check endpoint."""
    mongo_status = "connected" if db is not None else "not_connected"
    return {"status": "healthy", "service": "openai-twilio-bridge", "mongodb": mongo_status}


@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    """Handle incoming Twilio voice call - return TwiML to start Media Stream."""
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    protocol = request.headers.get("x-forwarded-proto", "https")
    ws_protocol = "wss" if protocol == "https" else "ws"
    
    # Get caller info from Twilio request (safely)
    caller_from = "unknown"
    try:
        form_data = await request.form()
        caller_from = form_data.get("From", "unknown")
    except Exception as e:
        logger.warning(f"Could not parse form data: {e}")
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_protocol}://{host}/media-stream">
            <Parameter name="from" value="{caller_from}" />
        </Stream>
    </Connect>
</Response>"""
    
    logger.info(f"Incoming call from {caller_from} - streaming to {ws_protocol}://{host}/media-stream")
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
