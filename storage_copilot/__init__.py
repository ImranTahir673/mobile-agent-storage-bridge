"""
Storage Copilot - On-Device Storage Bridge & Intelligent File Organizer
Package Version: v0.8.0
"""

from storage_copilot.app import create_app
from storage_copilot.config import APP_VERSION

__version__ = APP_VERSION
__all__ = ["create_app", "__version__"]
