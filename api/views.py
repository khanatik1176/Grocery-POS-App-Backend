import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import connection
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PasswordResetCode, Role
from .permissions import IsAdminRole
from .serializers import (
    AuthResponseSerializer,
    CreateUserSerializer,
    ForgotPasswordResponseSerializer,
    ForgotPasswordSerializer,
    HealthSerializer,
    LoginSerializer,
    ResetPasswordSerializer,
    RoleSerializer,
    UserSerializer,
)

User = get_user_model()
RESET_CODE_MINUTES = 15


@extend_schema(tags=["health"], responses={200: HealthSerializer})
@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:
        payload = {
            "status": "degraded",
            "database": "error",
            "engine": connection.vendor,
        }
        if settings.DEBUG:
            payload["detail"] = str(exc)
        return Response(payload, status=503)

    return Response(
        {
            "status": "ok",
            "database": "ok",
            "engine": connection.vendor,
        }
    )


class LoginView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["auth"],
        request=LoginSerializer,
        responses={200: AuthResponseSerializer},
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]
        user = User.objects.filter(email__iexact=email, is_active=True).select_related("profile__role").first()
        if user is None or not user.check_password(password) or not hasattr(user, "profile"):
            raise AuthenticationFailed("Invalid email or password.")
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": UserSerializer(user).data})


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["auth"], request=None, responses={204: None})
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["auth"], responses={200: UserSerializer})
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["auth"],
        request=ForgotPasswordSerializer,
        responses={200: ForgotPasswordResponseSerializer},
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        detail = "If that email is registered, a reset code is on its way."
        payload = {"detail": detail}
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            return Response(payload)

        PasswordResetCode.objects.filter(user=user, used=False).update(used=True)
        code = f"{secrets.randbelow(1_000_000):06d}"
        PasswordResetCode.objects.create(
            user=user,
            code=code,
            expires_at=timezone.now() + timedelta(minutes=RESET_CODE_MINUTES),
        )
        send_mail(
            subject="Grocery POS password reset",
            message=f"Your password reset code is {code}. It expires in {RESET_CODE_MINUTES} minutes.",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "grocery-pos@localhost"),
            recipient_list=[user.email],
        )
        if settings.DEBUG:
            payload["reset_code"] = code
        return Response(payload)


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(tags=["auth"], request=ResetPasswordSerializer, responses={200: dict})
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        reset = (
            PasswordResetCode.objects.filter(user=user, code=code, used=False).order_by("-created_at").first()
            if user
            else None
        )
        if reset is None or reset.expires_at < timezone.now():
            raise ValidationError({"code": "That reset code is invalid or expired."})
        user.set_password(serializer.validated_data["password"])
        user.save(update_fields=["password"])
        reset.used = True
        reset.save(update_fields=["used"])
        Token.objects.filter(user=user).delete()
        return Response({"detail": "Password updated. You can sign in."})


class RoleListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = RoleSerializer
    queryset = Role.objects.all()

    @extend_schema(tags=["admin"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(tags=["admin"])
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class UserListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    queryset = User.objects.filter(profile__isnull=False).select_related("profile__role")

    def get_serializer_class(self):
        if getattr(self, "request", None) is not None and self.request.method == "POST":
            return CreateUserSerializer
        return UserSerializer

    @extend_schema(tags=["admin"], responses={200: UserSerializer(many=True)})
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(tags=["admin"], request=CreateUserSerializer, responses={201: UserSerializer})
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)
