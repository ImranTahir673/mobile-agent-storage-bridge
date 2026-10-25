from .core_routes import core_bp
from .file_routes import file_bp
from .plan_routes import plan_bp
from .history_routes import history_bp
from .profile_routes import profile_bp
from .dedup_routes import dedup_bp
from .utility_routes import utility_bp

def register_blueprints(app):
    """Registers all application blueprints on the Flask app instance."""
    app.register_blueprint(core_bp)
    app.register_blueprint(file_bp)
    app.register_blueprint(plan_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(dedup_bp)
    app.register_blueprint(utility_bp)

__all__ = [
    "core_bp",
    "file_bp",
    "plan_bp",
    "history_bp",
    "profile_bp",
    "dedup_bp",
    "utility_bp",
    "register_blueprints",
]
