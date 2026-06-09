FROM python:3.12-slim

WORKDIR /app

# System deps for hmmlearn / numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code is mounted at runtime via docker-compose volumes,
# so no COPY of the .py files here. This keeps the image
# small and lets you edit code without rebuilding.

CMD ["python3", "daemon.py"]
