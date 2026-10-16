"""
Tests for sentry_config.py - Sentry error monitoring integration
All sentry_sdk imports are mocked since sentry_sdk may not be installed in test env
"""
import pytest
from unittest.mock import patch, MagicMock, call
import sys


@pytest.fixture(autouse=True)
def mock_sentry_sdk():
    """Mock sentry_sdk module for all tests in this file"""
    mock_sdk = MagicMock()
    mock_flask = MagicMock()
    mock_redis = MagicMock()
    mock_celery = MagicMock()

    mock_sdk.integrations.flask.FlaskIntegration = mock_flask.FlaskIntegration
    mock_sdk.integrations.redis.RedisIntegration = mock_redis.RedisIntegration
    mock_sdk.integrations.celery.CeleryIntegration = mock_celery.CeleryIntegration

    modules = {
        'sentry_sdk': mock_sdk,
        'sentry_sdk.integrations': MagicMock(),
        'sentry_sdk.integrations.flask': mock_flask,
        'sentry_sdk.integrations.redis': mock_redis,
        'sentry_sdk.integrations.celery': mock_celery,
    }

    with patch.dict(sys.modules, modules):
        # Force reimport of sentry_config with mocked modules
        if 'core.sentry' in sys.modules:
            del sys.modules['core.sentry']
        yield mock_sdk


class TestSentryInitialization:
    """Test Sentry initialization"""

    def test_sentry_init_with_dsn(self, mock_sentry_sdk):
        """Test Sentry initializes when DSN is set"""
        import core.sentry as sentry_config

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        with patch.dict('os.environ', {'SENTRY_DSN': 'https://test@sentry.io/123', 'FLASK_ENV': 'production', 'RENDER_GIT_COMMIT': 'abc1234'}):
            sentry_config.init_sentry(mock_app)

        mock_sentry_sdk.init.assert_called_once()
        mock_app.logger.info.assert_called()

    def test_sentry_init_without_dsn(self, mock_sentry_sdk, monkeypatch):
        """Test Sentry skips initialization when DSN is not set"""
        import core.sentry as sentry_config

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        # monkeypatch, rather than pop-and-restore: the old code restored only
        # `if old_val`, so the pinned empty DSN was dropped and never put back,
        # and the next load_dotenv() refilled SENTRY_DSN from the developer's
        # .env -- re-opening the hole conftest closes.
        monkeypatch.delenv('SENTRY_DSN', raising=False)
        monkeypatch.setenv('FLASK_ENV', 'production')
        sentry_config.init_sentry(mock_app)

        mock_sentry_sdk.init.assert_not_called()
        mock_app.logger.warning.assert_called()

    def test_sentry_init_default_environment(self, mock_sentry_sdk):
        """Test Sentry uses default environment"""
        import core.sentry as sentry_config

        mock_app = MagicMock()
        mock_app.logger = MagicMock()

        with patch.dict('os.environ', {'SENTRY_DSN': 'https://test@sentry.io/123', 'FLASK_ENV': 'production'}, clear=False):
            sentry_config.init_sentry(mock_app)

        mock_sentry_sdk.init.assert_called_once()


class TestFilterSensitiveData:
    """Test sensitive data filtering"""

    def test_filter_authorization_header(self, mock_sentry_sdk):
        """Test Authorization header is filtered"""
        import core.sentry as sentry_config

        event = {
            'request': {
                'headers': {
                    'Authorization': 'Bearer secret-token',
                    'Content-Type': 'application/json',
                }
            }
        }

        result = sentry_config.filter_sensitive_data(event, {})
        assert result['request']['headers']['Authorization'] == '[Filtered]'
        assert result['request']['headers']['Content-Type'] == 'application/json'

    def test_filter_cookie_header(self, mock_sentry_sdk):
        """Test Cookie header is filtered"""
        import core.sentry as sentry_config

        event = {
            'request': {
                'headers': {
                    'Cookie': 'session=secret123',
                }
            }
        }

        result = sentry_config.filter_sensitive_data(event, {})
        assert result['request']['headers']['Cookie'] == '[Filtered]'

    def test_filter_api_key_header(self, mock_sentry_sdk):
        """Test X-API-Key header is filtered"""
        import core.sentry as sentry_config

        event = {
            'request': {
                'headers': {
                    'X-API-Key': 'my-api-key',
                }
            }
        }

        result = sentry_config.filter_sensitive_data(event, {})
        assert result['request']['headers']['X-API-Key'] == '[Filtered]'

    def test_filter_password_in_post_data(self, mock_sentry_sdk):
        """Test password is filtered from POST data"""
        import core.sentry as sentry_config

        event = {
            'request': {
                'data': {
                    'email': 'test@example.com',
                    'password': 'secret123',
                }
            }
        }

        result = sentry_config.filter_sensitive_data(event, {})
        assert result['request']['data']['password'] == '[Filtered]'
        assert result['request']['data']['email'] == 'test@example.com'

    def test_filter_api_key_in_post_data(self, mock_sentry_sdk):
        """Test api_key is filtered from POST data"""
        import core.sentry as sentry_config

        event = {
            'request': {
                'data': {
                    'api_key': 'key123',
                    'name': 'test',
                }
            }
        }

        result = sentry_config.filter_sensitive_data(event, {})
        assert result['request']['data']['api_key'] == '[Filtered]'
        assert result['request']['data']['name'] == 'test'

    def test_filter_token_in_post_data(self, mock_sentry_sdk):
        """Test token and secret are filtered"""
        import core.sentry as sentry_config

        event = {
            'request': {
                'data': {
                    'token': 'tok123',
                    'secret': 'sec456',
                }
            }
        }

        result = sentry_config.filter_sensitive_data(event, {})
        assert result['request']['data']['token'] == '[Filtered]'
        assert result['request']['data']['secret'] == '[Filtered]'

    def test_filter_no_request(self, mock_sentry_sdk):
        """Test filter handles events without request"""
        import core.sentry as sentry_config

        event = {'message': 'test error'}
        result = sentry_config.filter_sensitive_data(event, {})
        assert result == event

    def test_filter_non_dict_data(self, mock_sentry_sdk):
        """Test filter handles non-dict POST data"""
        import core.sentry as sentry_config

        event = {
            'request': {
                'data': 'raw string data'
            }
        }
        result = sentry_config.filter_sensitive_data(event, {})
        assert result['request']['data'] == 'raw string data'


class TestCaptureException:
    """Test exception capture with context"""

    def test_capture_exception_with_context(self, mock_sentry_sdk):
        """Test capturing exception with additional context"""
        import core.sentry as sentry_config

        test_exception = ValueError("test error")
        context = {'user_id': 'user123', 'operation': 'pdf_upload'}

        sentry_config.capture_exception_with_context(test_exception, context)

        mock_sentry_sdk.push_scope.assert_called_once()

    def test_capture_exception_without_context(self, mock_sentry_sdk):
        """Test capturing exception without context"""
        import core.sentry as sentry_config

        test_exception = ValueError("test error")

        sentry_config.capture_exception_with_context(test_exception)

        mock_sentry_sdk.capture_exception.assert_called_once_with(test_exception)


class TestCaptureMessage:
    """Test message capture"""

    def test_capture_message_with_context(self, mock_sentry_sdk):
        """Test capturing message with context"""
        import core.sentry as sentry_config

        sentry_config.capture_message(
            "Test message",
            level='warning',
            context={'key': 'value'}
        )

        mock_sentry_sdk.push_scope.assert_called_once()

    def test_capture_message_without_context(self, mock_sentry_sdk):
        """Test capturing message without context"""
        import core.sentry as sentry_config

        sentry_config.capture_message("Test message", level='info')

        mock_sentry_sdk.capture_message.assert_called_once_with("Test message", level='info')

    def test_capture_message_default_level(self, mock_sentry_sdk):
        """Test capturing message with default level"""
        import core.sentry as sentry_config

        sentry_config.capture_message("Test message")

        mock_sentry_sdk.capture_message.assert_called_once_with("Test message", level='info')
