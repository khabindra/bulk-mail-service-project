from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import status

from .serializers import UserRegisterSerializer, UserProfileSerializer, MyTokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from drf_spectacular.utils import extend_schema, OpenApiExample, OpenApiResponse
from drf_spectacular.types import OpenApiTypes


class RegisterUserAPIView(APIView):
    """
    API endpoint to register a new Client user.
    Automatically creates a linked Client profile using the provided company name.
    """
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Authentication"],
        operation_id="register_user",
        request=UserRegisterSerializer,
        responses={
            201: OpenApiResponse(
                response=OpenApiTypes.OBJECT,
                description="User and Client profile created successfully.",
                examples=[OpenApiExample('Success', value={"message": "Client registered successfully"})]
            ),
            400: OpenApiResponse(
                description="Validation Error (e.g., missing fields, email already exists).",
                response=OpenApiTypes.OBJECT,
                examples=[
                    OpenApiExample('Missing Company', value={"company_name": ["This field is required."]}),
                    OpenApiExample('Email Exists', value={"email": ["This email is already registered as a client."]})
                ]
            )
        }
    )
    def post(self, request):
        serializer = UserRegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()
        return Response(
            {"message": "Client registered successfully"},
            status=status.HTTP_201_CREATED
        )


class UserProfileAPIView(APIView):
    """
    Retrieve the profile of the currently authenticated user.
    Returns ID, username, email, and role.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Users"],
        operation_id="get_user_profile",
        responses={
            200: UserProfileSerializer,
            401: OpenApiResponse(description="Authentication credentials were not provided.")
        }
    )
    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)


class MyTokenObtainPairView(TokenObtainPairView):
    """
    Custom Login endpoint.
    Returns standard JWT tokens, but enhanced with 'role' and 'username' in the response body 
    and 'role' embedded inside the access token payload.
    """
    serializer_class = MyTokenObtainPairSerializer

    @extend_schema(
        tags=["Authentication"],
        operation_id="custom_token_obtain",
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiResponse(
                description="Custom JWT response containing access/refresh tokens, plus user role and username.",
                examples=[
                    OpenApiExample(
                        'Login Success',
                        value={
                            'refresh': 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...',
                            'access': 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...',
                            'role': 'CLIENT',
                            'username': 'john_doe'
                        }
                    )
                ]
            ),
            401: OpenApiResponse(description="No active account found with the given credentials.")
        }
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)