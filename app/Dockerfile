FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
EXPOSE 8080
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
