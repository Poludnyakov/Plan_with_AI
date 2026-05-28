import sys
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtWebEngineWidgets import QWebEngineView

class CalendarApp(QMainWindow):
    """
    Native PyQt6 desktop application embedding the planiruy student visual calendar
    using the high-performance QWebEngineView.
    """
    def __init__(self, user_tg_id: int):
        super().__init__()
        self.user_tg_id = user_tg_id
        
        # Configure primary window metrics and metadata
        self.setWindowTitle("планиИруй! — Персональный Календарь Дедлайнов")
        self.resize(1200, 800)
        
        # Initialize Embedded Web Engine view
        self.web_view = QWebEngineView()
        self.setCentralWidget(self.web_view)
        
        # Connect signals for high-reliability connection checking
        self.web_view.loadFinished.connect(self.on_load_finished)
        
        # Load visual calendar dashboard
        self.load_calendar()

    def load_calendar(self):
        """Loads the visual calendar endpoint from the secure production server."""
        url_str = "https://planwithai.ru/calendar"
        self.web_view.setUrl(QUrl(url_str))

    def on_load_finished(self, ok: bool):
        """
        Triggered when a page loading operation completes.
        If the connection failed (e.g. backend server is not running),
        displays a gorgeous styled dark HTML recovery card instructing the user
        to start the uvicorn backend.
        """
        if not ok:
            error_html = """
            <!DOCTYPE html>
            <html lang="ru">
            <head>
                <meta charset="UTF-8">
                <style>
                    body {
                        background-color: #09090e;
                        color: #f3f1f8;
                        font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
                        text-align: center;
                        padding-top: 150px;
                        margin: 0;
                        background: radial-gradient(circle, rgba(22, 19, 48, 0.4) 0%, rgba(9, 9, 14, 1) 100%);
                    }
                    .container {
                        max-width: 600px;
                        margin: 0 auto;
                        padding: 40px;
                        background: rgba(22, 19, 48, 0.55);
                        border: 1px solid rgba(147, 51, 234, 0.25);
                        backdrop-filter: blur(16px);
                        border-radius: 20px;
                        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.05);
                    }
                    h1 {
                        color: #d946ef;
                        font-size: 28px;
                        margin-bottom: 20px;
                        text-shadow: 0 0 15px rgba(217, 70, 239, 0.4);
                        font-weight: 700;
                    }
                    p {
                        color: #a39eb9;
                        font-size: 16px;
                        line-height: 1.6;
                        margin-bottom: 30px;
                    }
                    .btn {
                        background: linear-gradient(135deg, #9333ea 0%, #d946ef 100%);
                        border: none;
                        color: white;
                        padding: 12px 30px;
                        text-decoration: none;
                        border-radius: 10px;
                        font-weight: bold;
                        font-size: 15px;
                        cursor: pointer;
                        box-shadow: 0 4px 15px rgba(217, 70, 239, 0.35);
                        transition: transform 0.2s, box-shadow 0.2s;
                    }
                    .btn:hover {
                        transform: translateY(-2px);
                        box-shadow: 0 6px 20px rgba(217, 70, 239, 0.5);
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <div style="font-size: 55px; margin-bottom: 20px;">⚠️</div>
                    <h1>Ошибка подключения</h1>
                    <p>Пожалуйста, проверьте подключение к интернету или статус сервера.<br>
                    Не удалось подключиться к защищенному серверу по адресу:<br>
                    <b style="color: #c084fc;">https://planwithai.ru</b></p>
                    <button class="btn" onclick="window.location.reload();">🔄 Повторить попытку</button>
                </div>
            </body>
            </html>
            """
            self.web_view.setHtml(error_html, QUrl("https://planwithai.ru/"))

if __name__ == "__main__":
    # Default fallback Telegram ID for testing
    user_tg_id = 12345
    if len(sys.argv) > 1:
        try:
            user_tg_id = int(sys.argv[1])
        except ValueError:
            print(f"Warning: Invalid Telegram ID argument. Using default fallback: {user_tg_id}")

    # Start PyQt Application Loop
    app = QApplication(sys.argv)
    window = CalendarApp(user_tg_id=user_tg_id)
    window.show()
    sys.exit(app.exec())
