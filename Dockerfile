FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# The ML stack is opt-in. It roughly triples the image, and the gateway only
# needs it when CLASSIFIER is logreg or xgb -- so the default image stays lean
# for the rules path, for CI, and for every kind node that pulls it in Phase 4.
#   docker compose build --build-arg INSTALL_ML=true
ARG INSTALL_ML=false

# Dependencies first, so application edits don't invalidate the layer.
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$INSTALL_ML" = "true" ]; then \
        pip install --no-cache-dir -r requirements-ml.txt; \
    fi

COPY app ./app

EXPOSE 8000

# Liveness is checked by the orchestrator, not by Docker, in every environment
# that matters. Kept here so `docker compose ps` is informative locally.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
