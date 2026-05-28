import pytest
from unittest.mock import AsyncMock, patch
import httpx
from services import SpeechKitService
from config import settings

@pytest.mark.anyio
async def test_speechkit_transcribe_success():
    """Tests that transcribe_voice returns transcription text on successful API response."""
    dummy_request = httpx.Request("POST", "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize")
    mock_response = httpx.Response(
        status_code=200,
        json={"result": "привет планируй"},
        request=dummy_request
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", "dummy_folder"):
            service = SpeechKitService()
            text = await service.transcribe_voice(b"fake_ogg_opus_bytes")
            
            assert text == "привет планируй"
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert args[0] == "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
            assert kwargs["headers"] == {"Authorization": "Api-Key dummy_key"}
            assert kwargs["params"] == {
                "folderId": "dummy_folder",
                "lang": "ru-RU",
                "format": "oggopus"
            }
            assert kwargs["content"] == b"fake_ogg_opus_bytes"


@pytest.mark.anyio
async def test_speechkit_empty_result():
    """Tests that transcribe_voice raises ValueError if SpeechKit returns empty text."""
    dummy_request = httpx.Request("POST", "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize")
    mock_response = httpx.Response(
        status_code=200,
        json={"result": "   "},
        request=dummy_request
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", "dummy_folder"):
            service = SpeechKitService()
            with pytest.raises(ValueError) as excinfo:
                await service.transcribe_voice(b"fake_ogg_opus_bytes")
            assert "empty transcription" in str(excinfo.value)


@pytest.mark.anyio
async def test_speechkit_http_error():
    """Tests that transcribe_voice raises HTTPStatusError when the API returns an error status code."""
    dummy_request = httpx.Request("POST", "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize")
    mock_response = httpx.Response(
        status_code=500,
        content=b"Internal Server Error",
        request=dummy_request
    )
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", "dummy_folder"):
            service = SpeechKitService()
            with pytest.raises(httpx.HTTPStatusError):
                await service.transcribe_voice(b"fake_ogg_opus_bytes")


@pytest.mark.anyio
async def test_speechkit_missing_credentials():
    """Tests that transcribe_voice raises ValueError if credentials are not configured."""
    with patch.object(settings, "API_KEY", None), patch.object(settings, "FOLDER_ID", "dummy_folder"):
        service = SpeechKitService()
        with pytest.raises(ValueError) as excinfo:
            await service.transcribe_voice(b"fake_bytes")
        assert "API_KEY is not configured" in str(excinfo.value)

    with patch.object(settings, "API_KEY", "dummy_key"), patch.object(settings, "FOLDER_ID", None):
        service = SpeechKitService()
        with pytest.raises(ValueError) as excinfo:
            await service.transcribe_voice(b"fake_bytes")
        assert "FOLDER_ID is not configured" in str(excinfo.value)


@pytest.mark.anyio
async def test_real_speechkit_connection():
    """
    Real integration test checking the connection and credentials for Yandex SpeechKit.
    Runs only if API_KEY and FOLDER_ID are present in settings.
    """
    if not settings.API_KEY or not settings.FOLDER_ID:
        pytest.skip("Yandex credentials are not configured in .env. Skipping real connection test.")
        
    service = SpeechKitService()
    # Sending a tiny dummy byte string to check if the connection authenticates successfully.
    # We expect either a successful transcription (unlikely for random bytes) or a 400 Bad Request
    # due to invalid audio format (which still proves successful authentication and connection!).
    try:
        # A small random byte string that is not a valid oggopus file
        dummy_audio = b"dummy_invalid_ogg_opus_audio_bytes_1234567890"
        await service.transcribe_voice(dummy_audio)
    except httpx.HTTPStatusError as e:
        # 400 Bad Request indicates successful connection and authentication, but invalid audio data.
        # This confirms that our FOLDER_ID and API_KEY are correct!
        if e.response.status_code == 400:
            print("\n[SPEECHKIT INTEGRATION] Connection successful! Yandex returned 400 Bad Request as expected for dummy audio.")
            assert True
        else:
            pytest.fail(f"Yandex SpeechKit returned unexpected HTTP status code {e.response.status_code}: {e.response.text}")
    except ValueError as e:
        # If the API returned empty string or something else
        if "empty transcription" in str(e):
            print("\n[SPEECHKIT INTEGRATION] Connection successful! Empty transcription received.")
            assert True
        else:
            pytest.fail(f"Unexpected ValueError occurred: {e}")
    except Exception as e:
        pytest.fail(f"Connection to Yandex SpeechKit failed completely with error: {e}")
