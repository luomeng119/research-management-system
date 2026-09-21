from app import create_app


def test_report_service_injection_is_available_without_database(tmp_path):
    service = object()
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "report-factory-test",
        "DATABASE_ENGINE": None,
        "AI_PROVIDER": "DISABLED",
        "SECURITY_AUTH_ENABLED": False,
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "FILE_STORAGE_ROOT": str(tmp_path / "files"),
        "RESEARCH_REPORT_SERVICE": service,
    })
    assert app.extensions["research_report_service"] is service
    assert app.config["RESEARCH_REPORTS_AVAILABLE"] is True


def test_report_entry_unavailable_without_service_or_schema(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "report-factory-test",
        "DATABASE_ENGINE": None,
        "AI_PROVIDER": "DISABLED",
        "SECURITY_AUTH_ENABLED": False,
        "SESSION_FILE_DIR": str(tmp_path / "sessions"),
        "FILE_STORAGE_ROOT": str(tmp_path / "files"),
    })
    assert app.config["RESEARCH_REPORTS_AVAILABLE"] is False


def test_report_draft_injection_is_exposed(tmp_path):
    draft = object()
    app = create_app({
        'TESTING': True, 'SECRET_KEY': 'report-factory-test',
        'DATABASE_ENGINE': None, 'AI_PROVIDER': 'DISABLED',
        'AI_FEATURES_VISIBLE': True,
        'SECURITY_AUTH_ENABLED': False,
        'SESSION_FILE_DIR': str(tmp_path / 'sessions'),
        'FILE_STORAGE_ROOT': str(tmp_path / 'files'),
        'RESEARCH_REPORT_DRAFT_SERVICE': draft,
    })
    assert app.extensions['research_report_draft_service'] is draft
    assert app.config['RESEARCH_REPORT_GENERATION_AVAILABLE'] is True
