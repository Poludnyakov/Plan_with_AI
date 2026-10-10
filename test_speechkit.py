import pytest
from unittest.mock import AsyncMock, patch

from services import SpeechKitService


@pytest.mark.anyio
async def test_speechkit_transcribe_success():
    with patch(
        "services.transcribe_audio", new_callable=AsyncMock,
        return_value="привет планируй",
    ) as transcribe:
        text = await SpeechKitService().transcribe_voice(b"fake_ogg_opus_bytes")

    assert text == "привет планируй"
    transcribe.assert_awaited_once_with(b"fake_ogg_opus_bytes", audio_format="ogg")


@pytest.mark.anyio
async def test_speechkit_transcription_error_propagates():
    with patch(
        "services.transcribe_audio", new_callable=AsyncMock,
        side_effect=ValueError("OpenRouter unavailable"),
    ):
        with pytest.raises(ValueError, match="OpenRouter unavailable"):
            await SpeechKitService().transcribe_voice(b"fake_ogg_opus_bytes")
