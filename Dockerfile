FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY newperson ./newperson
RUN pip install .
# 人设、照片、数据通过挂载进来
VOLUME ["/app/persona", "/app/data"]
CMD ["python", "-m", "newperson", "run"]
