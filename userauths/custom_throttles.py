from rest_framework.throttling import UserRateThrottle, AnonRateThrottle

# Strict throttle for login attempts (brute force prevention)
class LoginThrottle(UserRateThrottle):
    rate = "5/min"

class AnonLoginThrottle(AnonRateThrottle):
    rate = "10/min"


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
