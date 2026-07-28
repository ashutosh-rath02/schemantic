FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY schemantic schemantic
COPY ROS_Driver_for_Robots.pdf .

RUN pip install --no-cache-dir -e .

# .cache/ holds enrichment results, fetched datasheets, the workspace file,
# and the chat-memory SQLite DB -- all worth surviving a container restart,
# so it's a named volume in docker-compose.yml, not baked into the image.
VOLUME /app/.cache

EXPOSE 8000
CMD ["uvicorn", "schemantic.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
