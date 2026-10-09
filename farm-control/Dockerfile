FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY farm_control ./farm_control
RUN pip install --no-cache-dir . && useradd --system --uid 10001 farm
USER farm
CMD ["farm-control", "api"]
