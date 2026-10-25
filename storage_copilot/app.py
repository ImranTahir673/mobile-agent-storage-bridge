# storage_copilot/app.py
import traceback
from flask import Flask, request, jsonify
from storage_copilot import config
from storage_copilot.db.ledger import init_db
from storage_copilot.api import register_blueprints
from storage_copilot.security.auth import (
    get_active_auth_token,
    is_origin_allowed,
    verify_mutation_auth,
)


def create_app() -> Flask:
    """Flask application factory for Storage Copilot Mobile Storage Bridge."""
    app = Flask("MobileStorageBridge")

    # Initialize pre-shared Bearer token on startup
    get_active_auth_token()

    @app.before_request
    def security_gatekeeper():
        origin = request.headers.get("Origin")

        # Handle CORS preflight OPTIONS
        if request.method == "OPTIONS":
            if origin and not is_origin_allowed(origin):
                return jsonify({
                    "error": f"CORS policy blocked preflight from untrusted origin: {origin}"
                }), 403
            resp = app.make_default_options_response()
            if origin and is_origin_allowed(origin):
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Vary"] = "Origin"
            else:
                resp.headers["Access-Control-Allow-Origin"] = "http://127.0.0.1:8080"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-Copilot-Token, X-Requested-With, Accept"
            )
            return resp

        # Verify Bearer token and Origin on state-mutating endpoints
        is_ok, err_msg, status_code = verify_mutation_auth(request)
        if not is_ok:
            return jsonify({"error": err_msg, "status": "unauthorized"}), status_code

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin")
        if origin:
            if is_origin_allowed(origin):
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = (
                    "Content-Type, Authorization, X-Copilot-Token, X-Requested-With, Accept"
                )
        else:
            # Same-origin or non-browser CLI request: bind strictly to localhost rather than wildcard *
            response.headers["Access-Control-Allow-Origin"] = "http://127.0.0.1:8080"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-Copilot-Token, X-Requested-With, Accept"
            )
        return response

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