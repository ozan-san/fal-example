from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from pydantic import BaseModel, Field

from fal_telemetry.backends import Backend, LiveBackend, MockBackend
from fal_telemetry.budget import BudgetExhausted, RequestBudget
from fal_telemetry.config import Settings
from fal_telemetry.telemetry import setup_telemetry

settings = Settings()
providers = setup_telemetry(settings)
HTTPXClientInstrumentor().instrument()

# Imported after setup so its meters and tracers bind to the configured providers.
from fal_telemetry.service import GenerationError, GenerationService  # noqa: E402

backend: Backend
budget: RequestBudget | None
if settings.fal_mode == "live":
    backend = LiveBackend(settings.fal_key.get_secret_value())
    budget = RequestBudget(settings.fal_budget_file, settings.fal_max_requests)
else:
    backend = MockBackend()
    budget = None

service = GenerationService(settings, backend, budget)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    for provider in providers:
        provider.shutdown()


app = FastAPI(title="fal telemetry demo", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, excluded_urls="health")


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)


class GenerateResponse(BaseModel):
    request_id: str
    image_urls: list[str]
    queue_seconds: float
    inference_seconds: float | None
    total_seconds: float
    estimated_cost_usd: float
    trace_id: str


INDEX_HTML = (Path(__file__).parent / "index.html").read_text()
ERROR_STATUS = {"timeout": 504, "rate_limited": 503, "auth": 502}


@app.post("/generate", response_model=GenerateResponse)
async def generate(request: GenerateRequest) -> GenerateResponse:
    try:
        result = await service.generate(request.prompt)
    except BudgetExhausted as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except GenerationError as exc:
        # The upstream message stays in the logs; callers just get the category.
        status = ERROR_STATUS.get(exc.error_type, 502)
        raise HTTPException(status_code=status, detail=exc.error_type) from exc
    return GenerateResponse(
        request_id=result.request_id,
        image_urls=[image["url"] for image in result.images],
        queue_seconds=result.queue_seconds,
        inference_seconds=result.inference_seconds,
        total_seconds=result.total_seconds,
        estimated_cost_usd=result.estimated_cost_usd,
        trace_id=format(trace.get_current_span().get_span_context().trace_id, "032x"),
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return INDEX_HTML


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "mode": settings.fal_mode,
        "endpoint": settings.fal_endpoint,
        "budget_remaining": budget.remaining if budget else None,
    }
