# WhyCode 后端镜像:FastAPI + uvicorn
FROM python:3.11-slim

WORKDIR /app

# 先装依赖(层缓存):pyproject.toml + 源码
COPY pyproject.toml README.md ./
COPY agent/ ./agent/
COPY api/ ./api/
COPY cli/ ./cli/
COPY context/ ./context/
COPY eval/ ./eval/
COPY hooks/ ./hooks/
COPY mailbox/ ./mailbox/
COPY mcp_client/ ./mcp_client/
COPY policy/ ./policy/
COPY skills/ ./skills/
COPY store/ ./store/
COPY tools/ ./tools/
COPY verify/ ./verify/
COPY worktree/ ./worktree/

RUN pip install --no-cache-dir .

# uvicorn 服务(0.0.0.0 供 Docker 网络访问)
EXPOSE 8000
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
