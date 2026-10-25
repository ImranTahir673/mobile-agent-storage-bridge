import traceback
from flask import Flask, request, jsonify
from storage_copilot import config
from storage_copilot.db.ledger import init_db
from storage_copilot.api import register_blueprints

def create_app() -> Flask:
    """Flask application factory for Storage Copilot Mobile Storage Bridge."""
    app = Flask("MobileStorageBridge")

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept"
        return response

    @app.before_request
    def handle_options():
        if request.method == "OPTIONS":
            resp = app.make_default_options_response()
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept"
            return resp

    @app.errorhandler(Exception)
    def handle_global_exception(e):
        """Guarantees the bridge server always responds with JSON, never raw HTML error pages."""
        err_msg = str(e) or type(e).__name__
        print(f"[Storage Copilot Error] Unhandled Exception: {err_msg}")
        traceback.print_exc()
        return jsonify({
            "type": "conversation",
            "message": f"⚠️ Storage Copilot encountered an issue: {err_msg}",
            "error": err_msg,
            "version": config.APP_VERSION
        }), 500

    # Register API blueprints
    register_blueprints(app)

    # Initialize SQLite database schema
    init_db()

    return app
