import sys
from unittest.mock import MagicMock, patch
import pytest

# 1. Define dummy mock classes to represent PyQt6 classes
# so that inheritance and method patching work seamlessly without compiled C++ engines.
class MockQMainWindow:
    def setWindowTitle(self, title): pass
    def resize(self, w, h): pass
    def setCentralWidget(self, widget): pass
    def show(self): pass

class MockQWebEngineView:
    def __init__(self):
        self.loadFinished = MagicMock()
    def setUrl(self, url): pass
    def setHtml(self, html, url=None): pass

class MockQUrl:
    def __init__(self, url_str):
        self.url_str = url_str

# 2. Inject mock modules into sys.modules
mock_qt_widgets = MagicMock()
mock_qt_core = MagicMock()
mock_qt_webengine = MagicMock()

sys.modules['PyQt6'] = MagicMock()
sys.modules['PyQt6.QtWidgets'] = mock_qt_widgets
sys.modules['PyQt6.QtCore'] = mock_qt_core
sys.modules['PyQt6.QtWebEngineWidgets'] = mock_qt_webengine

# Bind our dummy classes to the mock modules
mock_qt_widgets.QMainWindow = MockQMainWindow
mock_qt_widgets.QApplication = MagicMock
mock_qt_webengine.QWebEngineView = MockQWebEngineView
mock_qt_core.QUrl = MockQUrl

# Now import the target GUI application class safely
from app_gui import CalendarApp

def test_calendar_app_init():
    """
    Verifies that CalendarApp initializes properly, configures window size,
    sets custom window title, and prepares QWebEngineView.
    """
    with patch.object(CalendarApp, "setWindowTitle") as mock_set_title, \
         patch.object(CalendarApp, "resize") as mock_resize, \
         patch.object(CalendarApp, "setCentralWidget") as mock_set_central, \
         patch.object(CalendarApp, "load_calendar") as mock_load:
         
        app = CalendarApp(user_tg_id=98765)
        
        # Verify setups
        assert app.user_tg_id == 98765
        mock_set_title.assert_called_once_with("планиИруй! — Персональный Календарь Дедлайнов")
        mock_resize.assert_called_once_with(1200, 800)
        mock_set_central.assert_called_once()
        mock_load.assert_called_once()


def test_calendar_app_load_url():
    """
    Verifies that load_calendar builds the correct URL containing the Telegram ID.
    """
    with patch.object(CalendarApp, "setWindowTitle"), \
         patch.object(CalendarApp, "resize"), \
         patch.object(CalendarApp, "setCentralWidget"):
         
        app = CalendarApp(user_tg_id=456)
        
        # Setup mock webview
        app.web_view = MagicMock()
        
        app.load_calendar()
        
        # Assert url setter triggered
        app.web_view.setUrl.assert_called_once()
        args, kwargs = app.web_view.setUrl.call_args
        
        # Check QUrl creation (our MockQUrl)
        assert isinstance(args[0], MockQUrl)
        assert args[0].url_str == "http://localhost:8000/calendar/456"


def test_calendar_app_offline_handling():
    """
    Verifies that when loading fails (ok is False), the QWebEngineView
    injects the custom dark styled recovery warning HTML block.
    """
    with patch.object(CalendarApp, "setWindowTitle"), \
         patch.object(CalendarApp, "resize"), \
         patch.object(CalendarApp, "setCentralWidget"), \
         patch.object(CalendarApp, "load_calendar"):
         
        app = CalendarApp(user_tg_id=123)
        app.web_view = MagicMock()
        
        # 1. Trigger load finished successfully
        app.on_load_finished(ok=True)
        app.web_view.setHtml.assert_not_called()
        
        # 2. Trigger load finished with failure (offline server)
        app.on_load_finished(ok=False)
        app.web_view.setHtml.assert_called_once()
        
        # Verify injected HTML has our required warning messages
        args, kwargs = app.web_view.setHtml.call_args
        html_content = args[0]
        assert "Ошибка подключения" in html_content
        assert "Пожалуйста, запустите FastAPI бэкенд (uvicorn)" in html_content
        assert "http://localhost:8000" in html_content
