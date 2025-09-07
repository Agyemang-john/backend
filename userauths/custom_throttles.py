from rest_framework.throttling import UserRateThrottle, AnonRateThrottle

# Strict throttle for login attempts (brute force prevention)
class LoginThrottle(UserRateThrottle):
    rate = "5/min"  # Max 5 attempts per minute per user

class AnonLoginThrottle(AnonRateThrottle):
    rate = "10/min"  # Max 10 attempts per minute per IP


# Checkout should not be spammed
class CheckoutThrottle(UserRateThrottle):
    rate = "20/hour"  # 20 orders per hour per user


# Password reset (avoid abuse)
class PasswordResetThrottle(AnonRateThrottle):
    rate = "3/min"  # very strict for security
