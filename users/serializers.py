from rest_framework import serializers
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


class UserRegisterSerializer(serializers.ModelSerializer):
    # ✅ ADDED: Swagger can now see this field and render an input box for it
    company_name = serializers.CharField(write_only=True, required=True)
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'password', 'company_name')

    # ✅ MOVED: Validation from View -> Serializer (Makes it visible to Swagger)
    def validate_email(self, value):
        from client.models import Client
        if Client.objects.filter(contact_email=value).exists():
            raise serializers.ValidationError("This email is already registered as a client.")
        return value

    def validate_company_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Company name is required.")
        return value.strip()

    def create(self, validated_data):
        # Extract company_name before creating the User
        company_name = validated_data.pop('company_name')
        
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email'),
            password=validated_data['password'],
            role='CLIENT'
        )
        
        # Create the linked Client profile
        from client.models import Client
        Client.objects.create(
            user=user,
            company_name=company_name,
            contact_email=user.email
        )
        return user


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'role')


class MyTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Add custom claims to the JWT token itself
        token['role'] = user.role
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        # Add custom fields to the LOGIN RESPONSE body
        data['role'] = self.user.role
        data['username'] = self.user.username
        return data