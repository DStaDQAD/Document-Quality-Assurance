# Hugging Face Spaces (Docker SDK) - serves the FastAPI app on port 7860.
FROM python:3.12-slim

# Run as a non-root user (Hugging Face Spaces convention).
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Copy the application code.
COPY --chown=user . .

# Hugging Face Spaces routes external traffic to this port; Render assigns its own via $PORT.
EXPOSE 7860

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}
