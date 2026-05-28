import pytest
import time
from anonymizer import DataAnonymizer


@pytest.fixture(scope="module")
def anonymizer():
    """
    Module-scoped fixture to instantiate DataAnonymizer once for all tests.
    Ensures that the heavy Natasha models are only loaded once, making the
    test suite execute extremely quickly.
    """
    return DataAnonymizer()


def test_empty_and_none_text(anonymizer):
    """Checks that empty inputs or whitespace are handled gracefully."""
    assert anonymizer.anonymize_text("") == ""
    assert anonymizer.anonymize_text("   ") == "   "


def test_no_pii_text(anonymizer):
    """Verifies that text without personal data is left unchanged."""
    normal_text = "Дедлайн по высшей математике перенесен на следующую пятницу. Группа 102, подготовьтесь!"
    assert anonymizer.anonymize_text(normal_text) == normal_text


def test_russian_fio_masking(anonymizer):
    """
    Tests various combinations of Russian names (FIO), including different
    grammatical cases (nominative, genitive, dative) and formats.
    """
    # Nominative Case (Именительный падеж)
    text1 = "Работу сдал Иванов Иван из группы 105."
    assert "Иванов Иван" not in anonymizer.anonymize_text(text1)
    assert "[ФИО]" in anonymizer.anonymize_text(text1)

    # Full FIO (ФИО полностью)
    text2 = "Смирнов Алексей Владимирович не пришел на пару."
    assert "Смирнов Алексей Владимирович" not in anonymizer.anonymize_text(text2)
    assert "[ФИО]" in anonymizer.anonymize_text(text2)

    # Dative Case (Дательный падеж - кому?)
    text3 = "Передай эту лабораторную работу Марии Петровне."
    assert "Марии Петровне" not in anonymizer.anonymize_text(text3)
    assert "[ФИО]" in anonymizer.anonymize_text(text3)

    # Genitive Case (Родительный падеж - у кого?)
    text4 = "Мы взяли конспект у Александра Козлова."
    assert "Александра Козлова" not in anonymizer.anonymize_text(text4)
    assert "[ФИО]" in anonymizer.anonymize_text(text4)


def test_phone_number_masking(anonymizer):
    """Tests different phone number formats common in the Russian Federation."""
    formats = [
        "89991234567",
        "+79991234567",
        "8-999-123-45-67",
        "+7-999-123-45-67",
        "8 (999) 123-45-67",
        "+7 (999) 123-45-67",
        "8 999 123 45 67",
        "+7 999 123 45 67",
    ]
    
    for phone in formats:
        text = f"Позвони мне по номеру {phone} для обсуждения проекта."
        anonymized = anonymizer.anonymize_text(text)
        assert phone not in anonymized
        assert "[ТЕЛЕФОН]" in anonymized


def test_email_masking(anonymizer):
    """Tests various email formats, including subdomains and plus addressing."""
    emails = [
        "student@university.edu",
        "ivanov.ivan@yandex.ru",
        "some_user+alias@gmail.com",
        "support@t.me",
    ]
    
    for email in emails:
        text = f"Сбрось методичку на почту {email} пожалуйста."
        anonymized = anonymizer.anonymize_text(text)
        assert email not in anonymized
        assert "[EMAIL]" in anonymized


def test_link_masking(anonymizer):
    """Tests HTTP/HTTPS protocols, tg protocols, and t.me short links."""
    links = [
        "http://google.com",
        "https://university.ru/departments/cs/schedule?day=monday",
        "tg://resolve?domain=planiruy_bot",
        "t.me/planiruy_channel/4321",
        "https://t.me/joinchat/ABC123xyz",
    ]
    
    for link in links:
        text = f"Вся информация доступна по ссылке {link}."
        anonymized = anonymizer.anonymize_text(text)
        assert link not in anonymized
        assert "[ССЫЛКА]" in anonymized


def test_mixed_student_queries(anonymizer):
    """Tests complete realistic student messages containing multiple mixed PII types."""
    query = (
        "Иванов Иван из группы 102 просил передать, что дедлайн перенесен на субботу. "
        "Свяжись со мной по почте ivanov@yandex.ru или по телефону +7 (999) 123-45-67. "
        "Ссылка на чат группы: https://t.me/joinchat/study102"
    )
    anonymized = anonymizer.anonymize_text(query)
    
    # Ensure all sensitive data types are masked
    assert "Иванов Иван" not in anonymized
    assert "ivanov@yandex.ru" not in anonymized
    assert "+7 (999) 123-45-67" not in anonymized
    assert "https://t.me/joinchat/study102" not in anonymized
    
    # Verify placeholders are correctly placed
    assert "[ФИО]" in anonymized
    assert "[EMAIL]" in anonymized
    assert "[ТЕЛЕФОН]" in anonymized
    assert "[ССЫЛКА]" in anonymized
    
    # Ensure regular context like group and day are preserved
    assert "группы 102" in anonymized
    assert "дедлайн перенесен" in anonymized


def test_performance(anonymizer):
    """
    Measures and asserts that the anonymization of a standard query is extremely fast
    (less than 50 milliseconds) once components are loaded in memory.
    """
    text = (
        "Мария Смирнова просила прислать ссылку на таблицу https://docs.google.com/spreadsheets/ "
        "для проверки домашнего задания. Ее почта smirnova.m@gmail.com, тел: 89001234567."
    )
    
    # Run once to warm up any lazy loading structures if any
    anonymizer.anonymize_text(text)
    
    # Measure execution time
    start_time = time.perf_counter()
    anonymizer.anonymize_text(text)
    duration = time.perf_counter() - start_time
    
    print(f"\n[PERFORMANCE] Anonymization took {duration * 1000:.3f} milliseconds.")
    
    # Assert execution duration is under 50ms (0.05 seconds) for extreme performance
    assert duration < 0.05, f"Anonymization is too slow: {duration:.3f}s"
