import asyncio
import httpx
from services import SpeechKitService
from config import settings

async def main():
    print("--- Real SpeechKit Transcription Test ---")
    if not settings.API_KEY or not settings.FOLDER_ID:
        print("Error: Yandex credentials not found in settings/.env!")
        return

    # Direct URL to a sample Opus file from ybrid/test-files (~4.6 KB)
    url = "https://raw.githubusercontent.com/ybrid/test-files/main/opus/short2.opus"
    print(f"Downloading sample Opus audio from: {url}")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            audio_bytes = response.content
            print(f"Successfully downloaded {len(audio_bytes)} bytes of Opus audio.")
        except Exception as e:
            print(f"Failed to download sample file: {e}")
            return

    # Call SpeechKitService to transcribe the audio bytes
    service = SpeechKitService()
    try:
        print("Sending audio to Yandex SpeechKit for transcription...")
        text = await service.transcribe_voice(audio_bytes)
        print("\n--- TRANSCRIPTION RESULT ---")
        print(text)
        print("----------------------------")
    except Exception as e:
        print(f"Transcription failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
