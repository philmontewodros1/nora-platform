"""
Startup and admin-visible safety checks.

Catches the exact two issues flagged in the system report: default admin credentials still
in place, and ADMIN_TELEGRAM_CHAT_ID unset (meaning the mismatch/stalled-delivery alerts
silently don't fire). These are cheap to check and easy to forget, so they're checked
automatically instead of relying on remembering a checklist.
"""
import os
import logging
import payments_guard

log = logging.getLogger("nora.security")


def run_checks() -> list[dict]:
    """Returns a list of {level, message} warnings. Call this at startup (log every entry
    loudly) and expose it via an admin-only endpoint so it's visible any time you check the
    dashboard, not just at deploy time when you might not be watching logs."""
    warnings = []

    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_pass = os.getenv("ADMIN_PASS", "changeme")
    if admin_user == "admin" or admin_pass == "changeme":
        warnings.append({
            "level": "critical",
            "message": "Admin dashboard is still using default credentials (admin/changeme or "
                       "similar). Anyone who finds this URL has full access to orders, shops, "
                       "and customer data. Set ADMIN_USER and ADMIN_PASS to real values now.",
        })

    if not os.getenv("ADMIN_TELEGRAM_CHAT_ID"):
        warnings.append({
            "level": "warning",
            "message": "ADMIN_TELEGRAM_CHAT_ID is not set. Stock-mismatch and stalled-delivery "
                       "alerts are built but have nowhere to send to -- they are silently not "
                       "firing. Get your numeric Telegram ID from @userinfobot and set it.",
        })

    payment_note = payments_guard.startup_warning()
    if payment_note:
        warnings.append({"level": "info", "message": payment_note})

    if os.getenv("DATABASE_URL", "").startswith("sqlite"):
        warnings.append({
            "level": "warning",
            "message": "DATABASE_URL is still SQLite. On Render's free tier this resets on "
                       "every redeploy. Point this at your Neon Postgres connection string "
                       "for anything beyond local testing.",
        })

    return warnings


def log_startup_warnings():
    for w in run_checks():
        prefix = {"critical": "CRITICAL", "warning": "WARNING", "info": "INFO"}.get(w["level"], "INFO")
        log.warning("[%s] %s", prefix, w["message"])
