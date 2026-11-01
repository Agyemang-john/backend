from django.urls import path, re_path

from . import views

urlpatterns = [
    path('products/<int:product_id>/questions/', views.ProductQuestionsView.as_view(), name='qa-product-questions'),
    path('questions/<int:pk>/', views.QuestionDetailView.as_view(), name='qa-question'),
    path('questions/<int:pk>/answers/', views.QuestionAnswersView.as_view(), name='qa-question-answers'),
    path('answers/<int:pk>/', views.AnswerDetailView.as_view(), name='qa-answer'),
    path('answers/<int:pk>/helpful/', views.AnswerHelpfulView.as_view(), name='qa-answer-helpful'),
    re_path(r'^(?P<kind>questions|answers)/(?P<pk>\d+)/report/$', views.ReportView.as_view(), name='qa-report'),
    path('seller/questions/', views.SellerQuestionsView.as_view(), name='qa-seller-questions'),
]
