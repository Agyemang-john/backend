# notification/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("ws-token/", views.get_websocket_token, name="ws-token"),
    path("list/", views.NotificationListView.as_view(), name="notification-list"),
    path("<int:id>/mark-read/", views.NotificationMarkReadView.as_view(), name="notification-mark-read"),
    path("mark-all-read/", views.NotificationMarkAllReadView.as_view(), name="notification-mark-all-read"),
    path('<int:id>/delete/', views.NotificationDeleteView.as_view(), name='notification-delete'),
    path('<int:id>/', views.NotificationDetailView.as_view(), name='notification-detail'),

    path('contact/', views.ContactInquiryCreateView.as_view(), name='contact-create'),
    path('report/', views.ReportCreateView.as_view(), name='report-create'),
    path('orders/', views.OrderListView.as_view(), name='order-list'),
]