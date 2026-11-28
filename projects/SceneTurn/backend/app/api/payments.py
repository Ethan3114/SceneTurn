from fastapi import APIRouter, Depends

from app.api.deps import current_user
from app.core.errors import AppError
from app.models import User
from app.schemas import PaymentCapabilities, PaymentNotification, PaymentOrder, PaymentRefund


router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/capabilities", response_model=PaymentCapabilities)
def capabilities(user: User = Depends(current_user)):
    return {"configured": False, "provider": None}


@router.post("/orders")
def create_order(body: PaymentOrder, user: User = Depends(current_user)):
    raise AppError(503, "PAYMENT_NOT_CONFIGURED", "支付渠道尚未配置")


@router.get("/orders/{order_id}")
def get_order(order_id: str, user: User = Depends(current_user)):
    raise AppError(503, "PAYMENT_NOT_CONFIGURED", "支付渠道尚未配置")


@router.post("/notifications")
def notification(body: PaymentNotification, user: User = Depends(current_user)):
    raise AppError(503, "PAYMENT_NOT_CONFIGURED", "支付渠道尚未配置")


@router.post("/refunds")
def refund(body: PaymentRefund, user: User = Depends(current_user)):
    raise AppError(503, "PAYMENT_NOT_CONFIGURED", "支付渠道尚未配置")
