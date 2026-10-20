from django.urls import path

from . import views

urlpatterns = [
    path('conversations/', views.ConversationListView.as_view(), name='chat-conversations'),
    path('conversations/<int:pk>/', views.ConversationDetailView.as_view(), name='chat-conversation'),
    path('conversations/<int:pk>/messages/', views.MessageListView.as_view(), name='chat-messages'),
    path('conversations/<int:pk>/read/', views.MarkReadView.as_view(), name='chat-read'),
    path('unread/', views.UnreadCountView.as_view(), name='chat-unread'),
    path('store/<slug:slug>/', views.StoreConversationView.as_view(), name='chat-store'),
]
