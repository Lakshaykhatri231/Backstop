"""
One real SMS to YOUR OWN phone, to prove the MSG91 adapter works with your
account. Nothing runs without --yes, and it refuses any number that is not
YOUR_TEST_MOBILE in .env (so it cannot be pointed at a customer).

    python scripts/live_check.py            # shows what it WOULD do, sends nothing
    python scripts/live_check.py --yes      # sends one OTP SMS through MSG91

Needs in .env: MSG91_AUTH_KEY, MSG91_OTP_TEMPLATE_ID (a DLT-approved OTP
template), YOUR_TEST_MOBILE. The code sent is random and not stored anywhere.
Exit code 0 only if MSG91 accepted the request. "Accepted" is not "delivered":
check your phone.
"""
import os
import secrets
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messaging                    # noqa: E402
from app.config import settings              # noqa: E402
from app.otp import OtpError, mask_phone, normalize_phone   # noqa: E402


def main() -> int:
    raw = os.getenv("YOUR_TEST_MOBILE", "")
    missing = [n for n, v in (("MSG91_AUTH_KEY", settings.msg91_auth_key),
                              ("MSG91_OTP_TEMPLATE_ID", settings.msg91_otp_template_id),
                              ("YOUR_TEST_MOBILE", raw)) if not v]
    if missing:
        print("Not ready - set in .env:", ", ".join(missing))
        return 2
    try:
        phone = normalize_phone(raw)
    except OtpError as e:
        print("YOUR_TEST_MOBILE is not a valid Indian mobile:", e.message)
        return 2
    print(f"Would send one OTP SMS to {mask_phone(phone)} via MSG91 template {settings.msg91_otp_template_id}.")
    if "--yes" not in sys.argv:
        print("Dry run. Re-run with --yes to actually send.")
        return 0
    code = f"{secrets.randbelow(10**6):06d}"
    res = messaging.msg91_send_otp(phone, code, max(1, settings.otp_ttl_seconds // 60))
    if res.ok:
        print(f"MSG91 accepted the request (ref {res.ref}). Check your phone for a 6-digit code.")
        return 0
    print("MSG91 rejected or could not be reached:", res.error)
    return 1


if __name__ == "__main__":
    sys.exit(main())
