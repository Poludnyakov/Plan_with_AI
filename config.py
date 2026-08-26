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
        
        # Load keys from environment
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
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
        self.APP_URL = os.getenv("APP_URL") or "http://localhost:8000"
        self.GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
        self.GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
        self.GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI") or f"{self.APP_URL}/google/callback"
        self.GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
        self.YANDEX_EMAIL = os.getenv("YANDEX_EMAIL")
        self.YANDEX_APP_PASSWORD = os.getenv("YANDEX_APP_PASSWORD")

# Singleton settings instance
settings = Settings()
