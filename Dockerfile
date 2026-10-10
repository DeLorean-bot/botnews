FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY botnews ./botnews
RUN pip install --no-cache-dir .
COPY sources.json ./
RUN useradd -m monitor && mkdir /app/data && chown -R monitor:monitor /app
USER monitor
CMD ["python", "-m", "botnews.main"]
