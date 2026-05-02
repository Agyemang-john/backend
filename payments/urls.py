from django.urls import path

from .momo_views import (
      BillingProfileView,
      MomoAccountListView, MomoAccountDetailView, SetDefaultMomoView,
      InitiateMomoView, PollMomoStatusView, ManualMomoPaymentView, SubmitMomoOtpView
  )
from . import views
from .flutterwave_view import FlutterwaveCallbackAPIView

from .billing_views import (
    BillingOverviewView, BillingHistoryView, BillingCardsView,
    SetDefaultCardView, DeleteCardView, ManualPaymentView, AddCardView, VerifyCardAddView,
)


app_name = 'payments'

urlpatterns = [

    path('verify-payment/<str:reference>/', views.VerifyPaymentAPIView.as_view(), name='verify_payment'),
    path('place-order-cod/', views.PlaceOrderCODAPIView.as_view(), name='place_order_cod'),
    path('flutterwave-callback/', FlutterwaveCallbackAPIView.as_view(), name='flutterwave-callback'),
    
    # ── Public ──────────────────────────────────────────────────────────────
    path(
        "plans/",
        views.SubscriptionPlanListView.as_view(),
        name="subscription-plans",
    ),

    # ── Subscription lifecycle ───────────────────────────────────────────────
    path(
        "initiate/",
        views.InitiateSubscriptionView.as_view(),
        name="subscription-initiate",
    ),
    path(
        "verify/",
        views.VerifySubscriptionView.as_view(),
        name="subscription-verify",
    ),
    path(
        "current/",
        views.CurrentSubscriptionView.as_view(),
        name="subscription-current",
    ),
    path(
        "cancel/",
        views.CancelSubscriptionView.as_view(),
        name="subscription-cancel",
    ),
    path(
        "auto-renew/",
        views.AutoRenewToggleView.as_view(),
        name="subscription-auto-renew",
    ),

    # ── Billing history ──────────────────────────────────────────────────────
    path(
        "payments/",
        views.PaymentHistoryView.as_view(),
        name="subscription-payments",
    ),

    # ── Saved cards ──────────────────────────────────────────────────────────
    path(
        "cards/",
        views.SavedCardsView.as_view(),
        name="subscription-cards",
    ),
    path(
        "cards/<int:card_id>/",
        views.SavedCardDetailView.as_view(),
        name="subscription-card-detail",
    ),
    path(
        "cards/<int:card_id>/set-default/",
        views.SetDefaultCardView.as_view(),
        name="subscription-card-set-default",
    ),

    path('billing/overview/',              BillingOverviewView.as_view()),
    path('billing/history/',               BillingHistoryView.as_view()),
    path('billing/cards/',                 BillingCardsView.as_view()),
    path('billing/cards/<int:pk>/default/', SetDefaultCardView.as_view()),
    path('billing/cards/<int:pk>/',        DeleteCardView.as_view()),
    path('billing/pay-now/',               ManualPaymentView.as_view()),
    path('billing/add-card/',              AddCardView.as_view()),
    path('billing/verify-card/',           VerifyCardAddView.as_view()),


    path('billing/profile/',              BillingProfileView.as_view()),
    path('momo/',                         MomoAccountListView.as_view()),
    path('momo/<int:pk>/',                MomoAccountDetailView.as_view()),
    path('momo/<int:pk>/default/',        SetDefaultMomoView.as_view()),
    path('momo/initiate/',                InitiateMomoView.as_view()),
    path('momo/status/',                  PollMomoStatusView.as_view()),
    path('momo/pay-now/',                 ManualMomoPaymentView.as_view()),
    path('momo/submit-otp/',              SubmitMomoOtpView.as_view()),


]
