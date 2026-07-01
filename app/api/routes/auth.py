from fastapi import APIRouter

from app.schemas import LoginRequest, LoginResponse


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    token = f"demo-token-for-{payload.email}"
    return LoginResponse(access_token=token, user_email=payload.email)
