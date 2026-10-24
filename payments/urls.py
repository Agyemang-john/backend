from django.urls import path

from .momo_views import (
      BillingProfileView,
      MomoAccountListView, MomoAccountDetailView, SetDefaultMomoView,
      InitiateMomoView, PollMomoStatusView, ManualMomoPaymentView, SubmitMomoOtpView
  )
from . import views

from .billing_views import (
    BillingOverviewView, BillingHistoryView, BillingCardsView,
    SetDefaultCardView, DeleteCardView, ManualPaymentView, AddCardView, VerifyCardAddView,
)


app_name = 'payments'

urlpatterns = [

    path('verify-payment/<str:reference>/', views.VerifyPaymentAPIView.as_view(), name='verify_payment'),
    path('place-order-cod/', views.PlaceOrderCODAPIView.as_view(), name='place_order_cod'),
    # Flutterwave isn't used at checkout. Its callback trusted the amount it was
    # sent and has no secret configured, so it stays unrouted until it's needed.
    
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
    path("quote/", views.PlanChangeQuoteView.as_view(), name="subscription-quote"),
    path("charge-card/", views.ChargeSavedCardView.as_view(), name="subscription-charge-card"),
    path("resume/", views.ResumeSubscriptionView.as_view(), name="subscription-resume"),
    path("scheduled-change/", views.ScheduledChangeView.as_view(), name="subscription-scheduled-change"),

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
