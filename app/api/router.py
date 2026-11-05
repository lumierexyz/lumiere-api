from fastapi import APIRouter

from app.ai.routes import router as ai_router
from app.economic_data.routes import router as economic_router
from app.expenses.routes import router as expenses_router
from app.forecasting.routes import router as forecasting_router
from app.portfolio.routes import router as portfolio_router
from app.risk.routes import router as risk_router
from app.transactions.routes import router as transactions_router
from app.users.routes import router as users_router

api_router = APIRouter()
for r in (
    users_router,
    transactions_router,
    expenses_router,
    economic_router,
    forecasting_router,
    risk_router,
    ai_router,
    portfolio_router,
):
    api_router.include_router(r)
