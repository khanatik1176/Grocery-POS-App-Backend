from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils.text import slugify
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Role, UserProfile

User = get_user_model()


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()
    database = serializers.CharField()
    engine = serializers.CharField()


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ["id", "name", "slug"]
        read_only_fields = ["id", "slug"]

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Enter a role name.")
        slug = slugify(name)
        if not slug:
            raise serializers.ValidationError("Enter a role name with letters or numbers.")
        if Role.objects.filter(slug=slug).exists():
            raise serializers.ValidationError("A role with this name already exists.")
        return name

    def create(self, validated_data):
        name = validated_data["name"]
        return Role.objects.create(name=name, slug=slugify(name))


class UserSerializer(serializers.ModelSerializer):
    role = RoleSerializer(source="profile.role", read_only=True)
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "role", "capabilities"]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_capabilities(self, user):
        from pos.capabilities import capabilities_for

        return capabilities_for(user)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class AuthResponseSerializer(serializers.Serializer):
    token = serializers.CharField()
    user = UserSerializer()


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ForgotPasswordResponseSerializer(serializers.Serializer):
    detail = serializers.CharField()
    reset_code = serializers.CharField(required=False)


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)
    password = serializers.CharField(write_only=True)

    def validate_password(self, value):
        validate_password(value)
        return value


class CreateUserSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    role_id = serializers.PrimaryKeyRelatedField(
        queryset=Role.objects.all(),
        source="role",
    )

    def validate_email(self, value):
        email = value.lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email

    def validate_password(self, value):
        validate_password(value)
        return value

    @transaction.atomic
    def create(self, validated_data):
        email = validated_data["email"]
        user = User.objects.create_user(
            username=email,
            email=email,
            password=validated_data["password"],
            first_name=validated_data["first_name"].strip(),
            last_name=validated_data.get("last_name", "").strip(),
        )
        UserProfile.objects.create(user=user, role=validated_data["role"])
        return user

    def to_representation(self, instance):
        return UserSerializer(instance).data
