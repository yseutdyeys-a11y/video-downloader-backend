FROM python:3.12-slim

# ============================================================
# PYTHON
# ============================================================

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1


# ============================================================
# SYSTEM PACKAGES
# ============================================================

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    unzip && \
    rm -rf /var/lib/apt/lists/*


# ============================================================
# DENO
# Required for modern YouTube JavaScript challenges
# ============================================================

RUN curl -fsSL https://deno.land/install.sh | sh


ENV DENO_INSTALL=/root/.deno

ENV PATH="/root/.deno/bin:${PATH}"


# ============================================================
# APPLICATION DIRECTORY
# ============================================================

WORKDIR /app


# ============================================================
# PYTHON DEPENDENCIES
# ============================================================

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt


# ============================================================
# APPLICATION
# ============================================================

COPY app ./app


# ============================================================
# DOWNLOAD DIRECTORY
# ============================================================

RUN mkdir -p /tmp/downloads


# ============================================================
# START SERVER
# ============================================================

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
