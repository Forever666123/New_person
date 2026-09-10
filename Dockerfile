FROM python:3.12-slim

# 时区只影响日志时间戳。她的作息用的是 persona.yaml 里的时区，
# 跟容器时区无关，所有时间计算都带时区信息。
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

COPY pyproject.toml README.md ./
COPY newperson ./newperson
RUN pip install . && \
    useradd --create-home --uid 10001 chloe && \
    mkdir -p /app/data && chown -R chloe:chloe /app

# 人设只读挂载，数据读写挂载。这里不写 VOLUME：
# 声明了的话 docker run 会自动建匿名卷，机器重装时人容易忘了它的存在，
# 记忆就悄悄没了。挂载显式写在 compose 里更难出事。
USER chloe

# 没有 tini 之类的初始化进程，所以她自己接 SIGTERM
STOPSIGNAL SIGTERM

CMD ["python", "-m", "newperson", "run"]
