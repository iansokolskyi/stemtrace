"""UI module - React SPA for task visualization."""

from celery_flow.server.ui.static import get_static_router, is_ui_available

__all__ = ["get_static_router", "is_ui_available"]
