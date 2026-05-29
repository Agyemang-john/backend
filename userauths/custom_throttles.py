from rest_framework.throttling import UserRateThrottle, AnonRateThrottle

class LoginThrottle(UserRateThrottle):
    scope = "login"
    rate = "5/min"

class AnonLoginThrottle(AnonRateThrottle):
    scope = "anon_login"
    rate = "10/min"

class RegisterThrottle(AnonRateThrottle):
    scope = "register"
    rate = "5/hour"


# Checkout should not be spammed
class CheckoutThrottle(UserRateThrottle):
    rate = "20/hour"


# Password reset (avoid abuse)
class PasswordResetThrottle(AnonRateThrottle):
    rate = "3/min"


# Automated heartbeat pings from the seller dashboard (every ~10 min).
# 30/hour gives plenty of room for page reloads and reconnects without
# allowing a runaway client to hammer the endpoint.
class VendorHeartbeatThrottle(UserRateThrottle):
    scope = "vendor_heartbeat"
    rate  = "30/hour"
