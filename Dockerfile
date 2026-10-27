FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*

COPY src/ ./src/
COPY aggregator.py ./aggregator.py

# Pre-compile the native C accelerator during image build
RUN python3 -c "from src.c_accel import is_c_engine_available; assert is_c_engine_available()"

ENTRYPOINT ["python3", "aggregator.py"]
CMD ["--help"]
