# CAA Financial Voice Agent - OpenAI Realtime

Voice assistant "Sarah" for CAA Financial using OpenAI Realtime API + Twilio.

## Why OpenAI Realtime?

- **Native g711_ulaw support** - No audio conversion needed with Twilio
- **Server VAD** - Automatic voice activity detection for natural turn-taking
- **Low latency** - Direct API integration

## Architecture

```
Twilio (mulaw 8kHz) <---> Bridge <---> OpenAI Realtime API
                     (no conversion!)
```

## Environment Variables

- `OPENAI_API_KEY` - OpenAI API key with Realtime API access
- `PORT` - Server port (default: 8000)

## Deployment

Deployed on Railway with automatic GitHub integration.

## Phone Number

+1 (720) 881-0564 (Twilio)
