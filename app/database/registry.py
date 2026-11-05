"""Import every model so ``Base.metadata`` is complete (Alembic, tests)."""

from app.database import results  # noqa: F401
from app.database.session import Base  # noqa: F401
from app.economic_data import models as economic_models  # noqa: F401
from app.transactions import models as transaction_models  # noqa: F401
from app.users import models as user_models  # noqa: F401
