import os

class Settings:
    """
    Settings class to load and hold configuration variables.
    Reads environment variables from a .env file if it exists,
    supporting both FOLDER_ID/API_KEY and YANDEX_FOLDER_ID/YANDEX_API_KEY names.
    """
    def __init__(self, env_file: str = ".env"):
        # Explicitly check for .env in the project directory
        # to ensure local development environments are loaded correctly.
        base_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(base_dir, env_file)
        
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        # Set environment variable if not already set
                        os.environ.setdefault(key, val)
        
        # OpenRouter is the single provider for text, file, image and audio
        # parsing. OPENROUTER_URL may be either the API root or the concrete
        # chat-completions URL supplied in the deployment environment.
        self.OPENROUTER_URL = (
            os.getenv("OPENROUTER_URL")
            or "https://openrouter.ai/api/v1/chat/completions"
        )
        self.OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
        self.OPENROUTER_MODEL_FOR_TEXT = os.getenv("OPENROUTER_MODEL_FOR_TEXT")
        self.OPENROUTER_MODEL_FOR_AUDIO = os.getenv("OPENROUTER_MODEL_FOR_AUDIO")
        self.OPENROUTER_MODEL_FOR_FILES = os.getenv("OPENROUTER_MODEL_FOR_FILES")
        # Document extraction should reserve its output budget for event JSON.
        self.OPENROUTER_DOCUMENT_REASONING_EFFORT = os.getenv(
            "OPENROUTER_DOCUMENT_REASONING_EFFORT", "none"
        ).strip().casefold()
        # A short message needs no hidden reasoning: with it the same model
        # answers in 20–90 s instead of 1–3 s and still makes the same mistakes.
        self.OPENROUTER_TEXT_REASONING_EFFORT = os.getenv(
            "OPENROUTER_TEXT_REASONING_EFFORT", "none"
        ).strip().casefold()
        # Spreadsheet fragments are independent and are read in parallel.
        try:
            self.OPENROUTER_DOCUMENT_CONCURRENCY = max(
                1, int(os.getenv("OPENROUTER_DOCUMENT_CONCURRENCY") or "4")
            )
        except ValueError:
            self.OPENROUTER_DOCUMENT_CONCURRENCY = 4
        self.OPENROUTER_MODEL_FOR_OCR = os.getenv("OPENROUTER_MODEL_FOR_OCR")
        self.OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER")
        self.OPENROUTER_APP_TITLE = os.getenv("OPENROUTER_APP_TITLE")

        # Legacy Yandex variables remain only for calendar synchronisation and
        # backwards-compatible local setups. AI parsers must use OpenRouter.
        self.API_KEY = os.getenv("YANDEX_API_KEY") or os.getenv("API_KEY")
        self.FOLDER_ID = os.getenv("FOLDER_IR") or os.getenv("FOLDER_ID") or os.getenv("YANDEX_FOLDER_ID")
        self.YANDEX_BASE_URL = os.getenv("YANDEX_BASE_URL") or "https://llm.api.cloud.yandex.net/v1"
        self.YANDEX_CLOUD_MODEL = os.getenv("YANDEX_CLOUD_MODEL") or "qwen3.6-35b-a3b/latest"
        self.YANDEX_DOCUMENT_MODEL = os.getenv("YANDEX_DOCUMENT_MODEL") or "yandexgpt-lite/latest"
        self.YANDEX_DOCUMENT_READER_MODEL = (
            os.getenv("YANDEX_DOCUMENT_READER_MODEL") or self.YANDEX_CLOUD_MODEL
        )
        self.YANDEX_DOCUMENT_NORMALIZER_MODEL = (
            os.getenv("YANDEX_DOCUMENT_NORMALIZER_MODEL")
            or self.YANDEX_DOCUMENT_MODEL
        )
        self.YANDEX_DOCUMENT_OCR_MODEL = (
            os.getenv("YANDEX_DOCUMENT_OCR_MODEL") or "table"
        )
        try:
            self.YANDEX_DOCUMENT_MAX_READER_FRAGMENTS = max(
                1, int(os.getenv("YANDEX_DOCUMENT_MAX_READER_FRAGMENTS") or "4")
            )
        except ValueError:
            self.YANDEX_DOCUMENT_MAX_READER_FRAGMENTS = 4
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
        # Brand custom emoji need Telegram Premium on the bot owner's account.
        self.TELEGRAM_CUSTOM_EMOJI = (
            os.getenv("TELEGRAM_CUSTOM_EMOJI", "1").strip().casefold() not in {"0", "false", "no", "off"}
        )
        self.APP_URL = os.getenv("APP_URL") or "http://localhost:8000"
        self.GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
        self.GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
        self.GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI") or f"{self.APP_URL}/google/callback"
        self.GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
        self.YANDEX_EMAIL = os.getenv("YANDEX_EMAIL")
        self.YANDEX_APP_PASSWORD = os.getenv("YANDEX_APP_PASSWORD")

# Singleton settings instance
settings = Settings()
