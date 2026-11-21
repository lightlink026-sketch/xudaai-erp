FROM python:3.12-slim
ARG ENABLE_CLIP=0
ARG ENABLE_MEDIA_AI=0
WORKDIR /srv/app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY requirements-clip.txt .
RUN if [ "$ENABLE_CLIP" = "1" ]; then pip install --no-cache-dir -r requirements-clip.txt; fi
COPY requirements-media.txt .
RUN if [ "$ENABLE_MEDIA_AI" = "1" ]; then apt-get update && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-chi-sim && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -r requirements-media.txt; fi
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
