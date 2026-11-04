FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.9.2
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "fal_telemetry.main:app", "--host", "0.0.0.0", "--port", "8000"]
